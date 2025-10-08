import os
import requests
import datetime
import logging
import json
import time
from datetime import timezone, timedelta
from dotenv import load_dotenv

# Slack
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

# Google
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# .envファイルから環境変数を読み込む
load_dotenv()

# ログ設定
logging.basicConfig(level=logging.INFO)


# --- .envファイルから認証情報を取得 ---
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
FREEEE_API_TOKEN = os.environ.get("FREEEE_API_TOKEN")
FREEEE_COMPANY_ID = os.environ.get("FREEEE_COMPANY_ID")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN")

# Slack Boltアプリを初期化
app = App(token=SLACK_BOT_TOKEN)


# ----------------------------------------------------
# 認証ヘルパー関数
# ----------------------------------------------------

def get_google_credentials():
    """リフレッシュトークンを使ってGoogle APIの認証情報を生成・更新する"""
    creds = Credentials.from_authorized_user_info(
        info={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": GOOGLE_REFRESH_TOKEN,
        },
        scopes=['https://www.googleapis.com/auth/calendar']
    )
    if not creds.valid and creds.expired and creds.refresh_token:
        logging.info("Googleの認証情報が期限切れのため、リフレッシュします...")
        creds.refresh(Request())
    return creds

# ----------------------------------------------------
# API連携ヘルパー関数
# ----------------------------------------------------

def get_email_from_slack(user_id, client):
    """SlackのユーザーIDからメールアドレスを取得"""
    try:
        result = client.users_info(user=user_id)
        return result["user"]["profile"]["email"]
    except SlackApiError as e:
        logging.error(f"Slackメール取得エラー: {e}")
        return None

def get_freee_employee_id_by_email(email):
    """メールアドレスからfreeeの従業員IDを取得"""
    url = f"https://api.freee.co.jp/hr/api/v1/companies/{FREEEE_COMPANY_ID}/employees"
    headers = {"Authorization": f"Bearer {FREEEE_API_TOKEN}"}
    params = {"email": email}
    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        employees = response.json()
        if employees:
            return employees[0]["id"]
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"freee従業員検索エラー: {e}")
        return None

def call_freee_time_clock(employee_id, clock_type, note=None):
    """freeeに打刻データを送信"""
    url = f"https://api.freee.co.jp/hr/api/v1/employees/{employee_id}/time_clocks"
    headers = {"Authorization": f"Bearer {FREEEE_API_TOKEN}", "Content-Type": "application/json"}
    now = datetime.datetime.now()
    data = {
        "company_id": int(FREEEE_COMPANY_ID),
        "type": clock_type,
        "base_date": now.strftime('%Y-%m-%d'),
        "datetime": now.strftime('%Y-%m-%d %H:%M:%S')
    }
    if note:
        data["note"] = note
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"freee打刻APIエラー: {e.response.text}")
        return False

def update_freee_attendance_tag(employee_id, date, tag_id):
    """freeeの勤怠タグを更新する"""
    url = f"https://api.freee.co.jp/hr/api/v1/employees/{employee_id}/work_records/{date}"
    headers = {"Authorization": f"Bearer {FREEEE_API_TOKEN}", "Content-Type": "application/json"}
    data = { "company_id": int(FREEEE_COMPANY_ID), "employee_attendance_tags": [{"attendance_tag_id": int(tag_id), "amount": 1}] }
    try:
        response = requests.put(url, headers=headers, json=data)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"freee勤怠タグ更新エラー: {e.response.text}")
        return False

def get_freee_leave_types(employee_id):
    """freeeから従業員が利用可能な休暇種別の一覧を取得する"""
    url = f"https://api.freee.co.jp/hr/api/v1/employees/{employee_id}/work_records/templates"
    headers = {"Authorization": f"Bearer {FREEEE_API_TOKEN}"}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        templates = response.json()
        return [{"id": t["id"], "name": t["name"]} for t in templates if t.get("category") == "leave"]
    except requests.exceptions.RequestException as e:
        logging.error(f"freee休暇種別取得エラー: {e}")
        return None

def submit_freee_leave_request(employee_id, leave_type_id, start_date, end_date):
    """freeeに休暇申請を送信（勤務記録を更新）"""
    current_date = datetime.datetime.strptime(start_date, '%Y-%m-%d').date()
    end_date_obj = datetime.datetime.strptime(end_date, '%Y-%m-%d').date()
    
    while current_date <= end_date_obj:
        date_str = current_date.strftime('%Y-%m-%d')
        url = f"https://api.freee.co.jp/hr/api/v1/employees/{employee_id}/work_records/{date_str}"
        headers = {"Authorization": f"Bearer {FREEEE_API_TOKEN}", "Content-Type": "application/json"}
        data = {"company_id": int(FREEEE_COMPANY_ID), "work_record_template_id": leave_type_id}
        try:
            response = requests.put(url, headers=headers, json=data)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logging.error(f"{date_str}のfreee休暇申請登録エラー: {e.response.text}")
            return False
        current_date += datetime.timedelta(days=1)
    return True

def add_event_to_google_calendar(summary, start_date, end_date):
    """Googleカレンダーに終日予定を追加"""
    try:
        creds = get_google_credentials()
        service = build('calendar', 'v3', credentials=creds)
        end_date_for_api = (datetime.datetime.strptime(end_date, '%Y-%m-%d') + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        event = {'summary': summary, 'start': {'date': start_date}, 'end': {'date': end_date_for_api}}
        service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        return True
    except Exception as e:
        logging.error(f"Googleカレンダー追加エラー: {e}")
        return False

# ----------------------------------------------------
# 共通ヘルパー
# ----------------------------------------------------
def get_employee_id_wrapper(user_id, client):
    email = get_email_from_slack(user_id, client)
    if not email:
        client.chat_postMessage(channel=user_id, text="エラー: Slackからメールアドレスを取得できませんでした。")
        return None
    employee_id = get_freee_employee_id_by_email(email)
    if not employee_id:
        client.chat_postMessage(channel=user_id, text=f"エラー: freeeにあなたの従業員情報が見つかりませんでした。(Email: {email})")
        return None
    return employee_id

# ----------------------------------------------------
# Slackコマンドハンドラー
# ----------------------------------------------------

@app.command("/出勤")
def handle_clock_in_command(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={"type": "modal", "callback_id": "clock_in_modal", "title": {"type": "plain_text", "text": "出勤打刻"},
              "submit": {"type": "plain_text", "text": "打刻"}, "blocks": [
                {"type": "input", "block_id": "location_block", "label": {"type": "plain_text", "text": "勤怠タグ"},
                 "element": {"type": "static_select", "action_id": "location_select", "placeholder": {"type": "plain_text", "text": "勤務形態を選択"},
                             "options": [
                                 {"text": {"type": "plain_text", "text": "🏠 在宅勤務"}, "value": "13548:在宅勤務"},
                                 {"text": {"type": "plain_text", "text": "🏢 本社勤務"}, "value": "3733:本社勤務"},
                                 {"text": {"type": "plain_text", "text": "💼 現場出社"}, "value": "3732:現場出社"},
                                 {"text": {"type": "plain_text", "text": "✈️ 出張"}, "value": "3734:出張"}
                             ]}}]}
    )

@app.command("/退勤")
def handle_clock_out_command(ack, body, client):
    ack()
    employee_id = get_employee_id_wrapper(body["user_id"], client)
    if employee_id and call_freee_time_clock(employee_id, "clock_out"):
        client.chat_postMessage(channel=body["user_id"], text="退勤打刻が完了しました。お疲れ様でした！")
    else:
        client.chat_postMessage(channel=body["user_id"], text="エラー: freeeへの打刻処理に失敗しました。")

@app.command("/各種申請")
def handle_applications_command(ack, body, client):
    """/各種申請 コマンドで、申請種別を選択するモーダルを開く"""
    ack()
    
    # 後続の処理で従業員IDが必要になるため、この時点で取得しておく
    employee_id = get_employee_id_wrapper(body["user_id"], client)
    if not employee_id:
        return # エラーメッセージはwrapper内で送信される

    # employee_idを後続のモーダルに渡すためにprivate_metadataに埋め込む
    view_private_metadata = {"employee_id": employee_id}

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "private_metadata": json.dumps(view_private_metadata),
            "callback_id": "select_application_type_view",
            "title": {"type": "plain_text", "text": "各種申請"},
            "submit": {"type": "plain_text", "text": "次へ"},
            "blocks": [
                {"type": "input", "block_id": "application_type_block", "label": {"type": "plain_text", "text": "申請種別"},
                 "element": {"type": "static_select", "action_id": "application_type_select", "placeholder": {"type": "plain_text", "text": "申請の種類を選択"},
                             "options": [
                                 {"text": {"type": "plain_text", "text": "有給休暇・特別休暇・欠勤"}, "value": "leave_request"},
                                 {"text": {"type": "plain_text", "text": "勤怠時間修正"}, "value": "time_correction"},
                                 {"text": {"type": "plain_text", "text": "休日出勤申請"}, "value": "holiday_work"},
                                 {"text": {"type": "plain_text", "text": "振替休日申請"}, "value": "compensatory_leave"},
                                 {"text": {"type": "plain_text", "text": "勤怠タグ修正"}, "value": "tag_correction"},
                             ]}}]}
    )

# ----------------------------------------------------
# Slackモーダルハンドラー
# ----------------------------------------------------

@app.view("clock_in_modal")
def handle_clock_in_submission(ack, body, client, view):
    ack()
    user_id = body["user"]["id"]
    selected_option = view["state"]["values"]["location_block"]["location_select"]["selected_option"]["value"]
    tag_id, tag_name = selected_option.split(':', 1)
    
    employee_id = get_employee_id_wrapper(user_id, client)
    if not employee_id:
        return

    if not call_freee_time_clock(employee_id, "clock_in"):
        client.chat_postMessage(channel=user_id, text="エラー: freeeへの打刻処理に失敗しました。")
        return
        
    # logging.info("freee側の処理を3秒待機します...")
    #time.sleep(3)
        
    today_str = datetime.date.today().isoformat()
    if update_freee_attendance_tag(employee_id, today_str, int(tag_id)):
        client.chat_postMessage(channel=user_id, text=f"出勤打刻と勤怠タグ「{tag_name}」の設定が完了しました！")
    else:
        client.chat_postMessage(channel=user_id, text="出勤打刻は完了しましたが、勤怠タグの更新に失敗しました。")

@app.view("select_application_type_view")
def handle_select_application_type(ack, body, client, view):
    """申請種別を選択後、専用のモーダルに切り替える"""
    ack()
    selected_type = view["state"]["values"]["application_type_block"]["application_type_select"]["selected_option"]["value"]
    
    # private_metadataからemployee_idを取得
    private_metadata = json.loads(view["private_metadata"])
    employee_id = private_metadata["employee_id"]
    
    today = datetime.date.today().isoformat()
    new_view_blocks = []
    callback_id = ""

    if selected_type == "leave_request":
        callback_id = "submit_leave_request_view"
        
        # employee_idを渡して関数を呼び出す
        leave_types = get_freee_leave_types(employee_id)
        if leave_types is None:
            # エラーモーダルを表示
            client.views_update(
                view_id=body["view"]["id"],
                hash=body["view"]["hash"],
                view={"type": "modal", "title": {"type": "plain_text", "text": "エラー"}, "blocks": [{"type": "section", "text": {"type": "plain_text", "text": "freeeから休暇種別を取得できませんでした。"}}]}
            )
            return
        
        options = [{"text": {"type": "plain_text", "text": leave["name"]}, "value": f"{leave['id']}:{leave['name']}"} for leave in leave_types]
        
        new_view_blocks = [
            {"type": "input", "block_id": "leave_type_block", "label": {"type": "plain_text", "text": "休暇種別"}, "element": {"type": "static_select", "action_id": "leave_type_select", "placeholder": {"type": "plain_text", "text": "休暇種別を選択"}, "options": options}},
            {"type": "input", "block_id": "start_date_block", "label": {"type": "plain_text", "text": "開始日"}, "element": {"type": "datepicker", "action_id": "start_date_picker", "initial_date": today}},
            {"type": "input", "block_id": "end_date_block", "label": {"type": "plain_text", "text": "終了日"}, "element": {"type": "datepicker", "action_id": "end_date_picker", "initial_date": today}},
            {"type": "input", "block_id": "reason_block", "label": {"type": "plain_text", "text": "詳細理由（任意）"}, "optional": True, "element": {"type": "plain_text_input", "action_id": "reason_input", "placeholder": {"type": "plain_text", "text": "例：通院のため"}, "multiline": True}}
        ]
    
    else:
        # 未実装の場合の処理
        client.views_update(
            view_id=body["view"]["id"],
            hash=body["view"]["hash"],
            view={"type": "modal", "title": {"type": "plain_text", "text": "エラー"}, "blocks": [{"type": "section", "text": {"type": "plain_text", "text": "この申請はまだ実装されていません。"}}]}
        )
        return

    # モーダルを新しい内容で更新（積み重ねる）
    client.views_push(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "private_metadata": json.dumps(private_metadata), # employee_idをさらに次のモーダルへ渡す
            "callback_id": callback_id,
            "title": {"type": "plain_text", "text": "申請内容の入力"},
            "submit": {"type": "plain_text", "text": "申請"},
            "blocks": new_view_blocks
        }
    )

@app.view("submit_leave_request_view")
def handle_submit_leave_request(ack, body, client, view):
    """休暇申請をfreeeに送信する"""
    ack()
    user_id = body["user"]["id"]
    values = body["view"]["state"]["values"]
    
    # private_metadataからemployee_idを取得
    private_metadata = json.loads(view["private_metadata"])
    employee_id = private_metadata["employee_id"]
    
    selected_option = values["leave_type_block"]["leave_type_select"]["selected_option"]["value"]
    leave_type_id, leave_type_name = selected_option.split(':', 1)
    start_date = values["start_date_block"]["start_date_picker"]["selected_date"]
    end_date = values["end_date_block"]["end_date_picker"]["selected_date"]

    if submit_freee_leave_request(employee_id, int(leave_type_id), start_date, end_date):
        client.chat_postMessage(channel=user_id, text=f"休暇申請をfreeeに提出しました。\n種別：{leave_type_name}\n期間：{start_date} ~ {end_date}\nfreee上で承認されるのをお待ちください。")
    else:
        client.chat_postMessage(channel=user_id, text="エラー: freeeへの休暇申請に失敗しました。")

# ----------------------------------------------------
# アプリケーション起動
# ----------------------------------------------------
if __name__ == "__main__":
    logging.info("🤖 WorkStamper is running!")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()