import os
import requests
import datetime
import logging
import json
import time
from dotenv import load_dotenv

# Slack
from slack_bolt import App, Ack
from slack_bolt.adapter.flask import SlackRequestHandler
from slack_sdk.errors import SlackApiError

# Google
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Web & DB
from flask import Flask, request, redirect
from tinydb import TinyDB, Query

# .envファイルから環境変数を読み込む
load_dotenv()
# ログ設定
logging.basicConfig(level=logging.INFO)


# --- .envファイルから認証情報を取得 ---
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
FREEEE_COMPANY_ID = os.environ.get("FREEEE_COMPANY_ID")
FREEEE_CLIENT_ID = os.environ.get("FREEEE_CLIENT_ID")
FREEEE_CLIENT_SECRET = os.environ.get("FREEEE_CLIENT_SECRET")
FREEEE_REDIRECT_URI = os.environ.get("FREEEE_REDIRECT_URI")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN")

# アプリの初期化
app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
flask_app = Flask(__name__)
handler = SlackRequestHandler(app)
db = TinyDB('user_tokens.json')
UserToken = Query()

# ----------------------------------------------------
# 認証ヘルパー関数
# ----------------------------------------------------

def get_google_credentials():
    creds = Credentials.from_authorized_user_info(info={"client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET, "refresh_token": GOOGLE_REFRESH_TOKEN}, scopes=['https://www.googleapis.com/auth/calendar'])
    if not creds.valid and creds.expired and creds.refresh_token:
        logging.info("Googleの認証情報が期限切れのため、リフレッシュします...")
        creds.refresh(Request())
    return creds

def get_freee_token(slack_user_id):
    user_data = db.get(UserToken.slack_user_id == slack_user_id)
    if not user_data: return None
    expiry_time = datetime.datetime.fromtimestamp(user_data.get('created_at', 0) + user_data.get('expires_in', 0))
    if datetime.datetime.now() >= expiry_time:
        logging.info(f"freeeアクセストークンが期限切れです。ユーザー: {slack_user_id}")
        token_url = "https://accounts.secure.freee.co.jp/public_api/token"
        payload = {"grant_type": "refresh_token", "client_id": FREEEE_CLIENT_ID, "client_secret": FREEEE_CLIENT_SECRET, "refresh_token": user_data['refresh_token']}
        try:
            response = requests.post(token_url, data=payload)
            response.raise_for_status()
            new_token_data = response.json()
            db.update(new_token_data, UserToken.slack_user_id == slack_user_id)
            return new_token_data.get('access_token')
        except requests.exceptions.RequestException as e:
            logging.error(f"freeeトークンのリフレッシュに失敗: {e}")
            return None
    else:
        return user_data.get('access_token')

# ----------------------------------------------------
# API連携ヘルパー関数
# ----------------------------------------------------

def get_email_from_slack(user_id, client):
    try:
        result = client.users_info(user=user_id)
        return result["user"]["profile"]["email"]
    except SlackApiError as e:
        logging.error(f"Slackメール取得エラー: {e}")
        return None

def get_freee_employee_id_by_email(email, access_token):
    url = f"https://api.freee.co.jp/hr/api/v1/companies/{FREEEE_COMPANY_ID}/employees"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"email": email}
    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        employees = response.json()
        if employees: return employees[0].get("id")
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"freee従業員検索エラー: {e}")
        return None

def call_freee_time_clock(employee_id, clock_type, access_token, note=None):
    url = f"https://api.freee.co.jp/hr/api/v1/employees/{employee_id}/time_clocks"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    now = datetime.datetime.now()
    data = {"company_id": int(FREEEE_COMPANY_ID), "type": clock_type, "base_date": now.strftime('%Y-%m-%d'), "datetime": now.strftime('%Y-%m-%d %H:%M:%S')}
    if note: data["note"] = note
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"freee打刻APIエラー: {e.response.text}")
        return False

def update_freee_attendance_tag(employee_id, date, tag_id, access_token):
    url = f"https://api.freee.co.jp/hr/api/v1/employees/{employee_id}/work_records/{date}"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        get_response = requests.get(url, headers={"Authorization": f"Bearer {access_token}"})
        get_response.raise_for_status()
        work_record = get_response.json()
        work_record["employee_attendance_tags"] = [{"attendance_tag_id": int(tag_id), "amount": 1}]
        work_record["company_id"] = int(FREEEE_COMPANY_ID)
        put_response = requests.put(url, headers=headers, json=work_record)
        put_response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"freee勤怠タグ更新エラー: {e.response.text}")
        return False

def get_freee_leave_types(access_token):
    url = f"https://api.freee.co.jp/hr/api/v1/companies/{FREEEE_COMPANY_ID}/work_record_templates"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        templates = response.json()
        return [{"id": t["id"], "name": t["name"]} for t in templates if t.get("category") == "leave"]
    except requests.exceptions.RequestException as e:
        logging.error(f"freee休暇種別取得エラー: {e}")
        return None

def submit_freee_leave_request(employee_id, leave_type_id, start_date, end_date, access_token):
    current_date = datetime.datetime.strptime(start_date, '%Y-%m-%d').date()
    end_date_obj = datetime.datetime.strptime(end_date, '%Y-%m-%d').date()
    while current_date <= end_date_obj:
        date_str = current_date.strftime('%Y-%m-%d')
        url = f"https://api.freee.co.jp/hr/api/v1/employees/{employee_id}/work_records/{date_str}"
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        data = {"company_id": int(FREEEE_COMPANY_ID), "work_record_template_id": leave_type_id}
        try:
            response = requests.put(url, headers=headers, json=data)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logging.error(f"{date_str}のfreee休暇申請登録エラー: {e.response.text}")
            return False
        current_date += datetime.timedelta(days=1)
    return True

# ----------------------------------------------------
# 共通ヘルパー
# ----------------------------------------------------
def get_employee_id_from_slack_id(user_id, client, access_token):
    email = get_email_from_slack(user_id, client)
    if not email:
        client.chat_postMessage(channel=user_id, text="エラー: Slackメールアドレス取得不可")
        return None
    employee_id = get_freee_employee_id_by_email(email, access_token)
    if not employee_id:
        client.chat_postMessage(channel=user_id, text=f"エラー: freee従業員情報が見つかりません(Email: {email})")
        return None
    return employee_id

def pre_check_authentication(user_id, client):
    if not db.contains(UserToken.slack_user_id == user_id):
        state = user_id
        auth_url = (f"https://accounts.secure.freee.co.jp/public_api/authorize"
                    f"?client_id={FREEEE_CLIENT_ID}&redirect_uri={FREEEE_REDIRECT_URI}"
                    f"&response_type=code&state={state}")
        client.chat_postMessage(channel=user_id, text=f"このコマンドを使用するには、まずfreeeアカウントとの連携が必要です。\n{auth_url}")
        return False
    return True

# ----------------------------------------------------
# Slackコマンドハンドラー
# ----------------------------------------------------
@app.command("/連携")
def handle_auth_command(ack, body, client):
    ack()
    state = body["user_id"]
    auth_url = (f"https://accounts.secure.freee.co.jp/public_api/authorize"
                f"?client_id={FREEEE_CLIENT_ID}&redirect_uri={FREEEE_REDIRECT_URI}"
                f"&response_type=code&state={state}")
    client.chat_postMessage(channel=body["user_id"], text=f"WorkStamperとfreeeアカウントを連携してください。\n{auth_url}")

@app.command("/出勤")
def handle_clock_in_command(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not pre_check_authentication(user_id, client): return
    client.views_open(trigger_id=body["trigger_id"], view={"type": "modal", "callback_id": "clock_in_modal", "title": {"type": "plain_text", "text": "出勤打刻"}, "submit": {"type": "plain_text", "text": "打刻"}, "blocks": [{"type": "input", "block_id": "location_block", "label": {"type": "plain_text", "text": "勤怠タグ"}, "element": {"type": "static_select", "action_id": "location_select", "placeholder": {"type": "plain_text", "text": "勤務形態を選択"}, "options": [{"text": {"type": "plain_text", "text": "🏠 在宅勤務"}, "value": "13548:在宅勤務"}, {"text": {"type": "plain_text", "text": "🏢 本社勤務"}, "value": "3733:本社勤務"}, {"text": {"type": "plain_text", "text": "💼 現場出社"}, "value": "3732:現場出社"}, {"text": {"type": "plain_text", "text": "✈️ 出張"}, "value": "3734:出張"}]}}]})

@app.command("/退勤")
def handle_clock_out_command(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not pre_check_authentication(user_id, client): return
    access_token = get_freee_token(user_id)
    if not access_token:
        client.chat_postMessage(channel=user_id, text="エラー: freeeの認証が切れています。`/連携`コマンドを再実行してください。")
        return
    employee_id = get_employee_id_from_slack_id(user_id, client, access_token)
    if employee_id and call_freee_time_clock(employee_id, "clock_out", access_token):
        client.chat_postMessage(channel=user_id, text="退勤打刻が完了しました。お疲れ様でした！")
    else:
        client.chat_postMessage(channel=user_id, text="エラー: freeeへの打刻処理に失敗しました。")


# ★★★ ここからが修正箇所 ★★★

def open_application_modal(client, body, logger):
    """モーダルを開く実際の処理（時間のかかる処理を含む）"""
    user_id = body["user_id"]
    try:
        access_token = get_freee_token(user_id)
        if not access_token:
            client.chat_postMessage(channel=user_id, text="エラー: freeeの認証が切れています。`/連携`コマンドを再実行してください。")
            return
            
        employee_id = get_employee_id_from_slack_id(user_id, client, access_token)
        if not employee_id: return

        view_private_metadata = {"employee_id": employee_id}
        client.views_open(
            trigger_id=body["trigger_id"],
            view={"type": "modal", "private_metadata": json.dumps(view_private_metadata), "callback_id": "select_application_type_view", "title": {"type": "plain_text", "text": "各種申請"}, "submit": {"type": "plain_text", "text": "次へ"}, "blocks": [{"type": "input", "block_id": "application_type_block", "label": {"type": "plain_text", "text": "申請種別"}, "element": {"type": "static_select", "action_id": "application_type_select", "placeholder": {"type": "plain_text", "text": "申請の種類を選択"}, "options": [{"text": {"type": "plain_text", "text": "有給休暇・特別休暇・欠勤"}, "value": "leave_request"}, {"text": {"type": "plain_text", "text": "勤怠時間修正"}, "value": "time_correction"}]}}]}
        )
    except Exception as e:
        logger.error(f"モーダル表示エラー: {e}")

@app.command("/各種申請")
def handle_applications_command(ack: Ack, body: dict, client, logger):
    """/各種申請 コマンドを受け取り、重い処理を分離する"""
    user_id = body["user_id"]
    # 認証チェックだけを先に行う
    if not pre_check_authentication(user_id, client):
        ack()
        return

    # 3秒以内にack()を返す
    ack()
    
    # 時間のかかる処理をバックグラウンドで実行
    # (Cloud Run環境ではスレッドの挙動が異なる場合があるため、直接呼び出す)
    open_application_modal(client, body, logger)

# ★★★ ここまでが修正箇所 ★★★


# ----------------------------------------------------
# Slackモーダルハンドラー
# ----------------------------------------------------

@app.view("clock_in_modal")
def handle_clock_in_submission(ack, body, client, view):
    ack()
    user_id = body["user"]["id"]
    selected_option = view["state"]["values"]["location_block"]["location_select"]["selected_option"]["value"]
    tag_id, tag_name = selected_option.split(':', 1)
    
    access_token = get_freee_token(user_id)
    if not access_token:
        client.chat_postMessage(channel=user_id, text="エラー: freeeの認証情報がありません。`/連携`コマンドを実行してください。")
        return
    
    employee_id = get_employee_id_from_slack_id(user_id, client, access_token)
    if not employee_id: return

    if not call_freee_time_clock(employee_id, "clock_in", access_token):
        client.chat_postMessage(channel=user_id, text="エラー: freeeへの打刻処理に失敗しました。")
        return
        
    logging.info("freee側の処理を3秒待機します...")
    time.sleep(3)
        
    today_str = datetime.date.today().isoformat()
    if update_freee_attendance_tag(employee_id, today_str, int(tag_id), access_token):
        client.chat_postMessage(channel=user_id, text=f"出勤打刻と勤怠タグ「{tag_name}」の設定が完了しました！")
    else:
        client.chat_postMessage(channel=user_id, text="出勤打刻は完了しましたが、勤怠タグの更新に失敗しました。")

@app.view("select_application_type_view")
def handle_select_application_type(ack, body, client, view):
    ack()
    user_id = body["user"]["id"]
    selected_type = view["state"]["values"]["application_type_block"]["application_type_select"]["selected_option"]["value"]
    private_metadata = json.loads(view["private_metadata"])
    employee_id = private_metadata["employee_id"]
    
    today = datetime.date.today().isoformat()
    new_view_blocks = []
    callback_id = ""

    access_token = get_freee_token(user_id)
    if not access_token: return

    if selected_type == "leave_request":
        callback_id = "submit_leave_request_view"
        leave_types = get_freee_leave_types(access_token) # employee_idは不要
        if leave_types is None:
            client.views_update(view_id=body["view"]["id"], hash=body["view"]["hash"], view={"type": "modal", "title": {"type": "plain_text", "text": "エラー"}, "blocks": [{"type": "section", "text": {"type": "plain_text", "text": "freeeから休暇種別を取得できませんでした。"}}]} )
            return
        
        options = [{"text": {"type": "plain_text", "text": leave["name"]}, "value": f"{leave['id']}:{leave['name']}"} for leave in leave_types]
        new_view_blocks = [{"type": "input", "block_id": "leave_type_block", "label": {"type": "plain_text", "text": "休暇種別"}, "element": {"type": "static_select", "action_id": "leave_type_select", "placeholder": {"type": "plain_text", "text": "休暇種別を選択"}, "options": options}}, {"type": "input", "block_id": "start_date_block", "label": {"type": "plain_text", "text": "開始日"}, "element": {"type": "datepicker", "action_id": "start_date_picker", "initial_date": today}}, {"type": "input", "block_id": "end_date_block", "label": {"type": "plain_text", "text": "終了日"}, "element": {"type": "datepicker", "action_id": "end_date_picker", "initial_date": today}}]
    
    else: # 未実装
        client.views_update(view_id=body["view"]["id"], hash=body["view"]["hash"], view={"type": "modal", "title": {"type": "plain_text", "text": "エラー"}, "blocks": [{"type": "section", "text": {"type": "plain_text", "text": "この申請はまだ実装されていません。"}}]} )
        return

    client.views_push(trigger_id=body["trigger_id"], view={"type": "modal", "private_metadata": json.dumps(private_metadata), "callback_id": callback_id, "title": {"type": "plain_text", "text": "申請内容の入力"}, "submit": {"type": "plain_text", "text": "申請"}, "blocks": new_view_blocks})

@app.view("submit_leave_request_view")
def handle_submit_leave_request(ack, body, client, view):
    ack()
    user_id = body["user"]["id"]
    values = body["view"]["state"]["values"]
    private_metadata = json.loads(view["private_metadata"])
    employee_id = private_metadata["employee_id"]
    
    selected_option = values["leave_type_block"]["leave_type_select"]["selected_option"]["value"]
    leave_type_id, leave_type_name = selected_option.split(':', 1)
    start_date = values["start_date_block"]["start_date_picker"]["selected_date"]
    end_date = values["end_date_block"]["end_date_picker"]["selected_date"]

    access_token = get_freee_token(user_id)
    if not access_token:
        client.chat_postMessage(channel=user_id, text="エラー: freeeの認証情報がありません。`/連携`コマンドを実行してください。")
        return

    if submit_freee_leave_request(employee_id, int(leave_type_id), start_date, end_date, access_token):
        client.chat_postMessage(channel=user_id, text=f"休暇申請をfreeeに提出しました。\n種別：{leave_type_name}\n期間：{start_date} ~ {end_date}\nfreee上で承認されるのをお待ちください。")
    else:
        client.chat_postMessage(channel=user_id, text="エラー: freeeへの休暇申請に失敗しました。")

# ----------------------------------------------------
# Flaskエンドポイント & アプリケーション起動
# ----------------------------------------------------

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)

@flask_app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    code = request.args.get("code")
    slack_user_id = request.args.get("state")
    
    token_url = "https://accounts.secure.freee.co.jp/public_api/token"
    payload = {
        "grant_type": "authorization_code", "client_id": FREEEE_CLIENT_ID,
        "client_secret": FREEEE_CLIENT_SECRET, "code": code, "redirect_uri": FREEEE_REDIRECT_URI,
    }
    response = requests.post(token_url, data=payload)
    token_data = response.json()

    if "access_token" in token_data:
        token_data['slack_user_id'] = slack_user_id
        db.upsert(token_data, UserToken.slack_user_id == slack_user_id)
        app.client.chat_postMessage(channel=slack_user_id, text="freeeとの連携が完了しました！")
        return "連携が完了しました。このウィンドウを閉じてください。"
    else:
        logging.error(f"freee token exchange failed for user {slack_user_id}: {token_data}")
        return "エラーが発生しました。連携に失敗しました。", 500

# ローカルでの開発用にSocket Modeで起動するためのコード
if __name__ == "__main__":
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
    logging.info("🤖 WorkStamper is running in Socket Mode!")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()