import os
import requests
import datetime
import logging
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# .envファイルから環境変数を読み込む
load_dotenv()

# ログの設定
logging.basicConfig(level=logging.INFO)


# --- .envファイルから認証情報を取得 ---
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
FREEEE_API_TOKEN = os.environ.get("FREEEE_API_TOKEN")
FREEEE_COMPANY_ID = os.environ.get("FREEEE_COMPANY_ID")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID")
GOOGLE_ACCESS_TOKEN = os.environ.get("GOOGLE_ACCESS_TOKEN")
APPROVER_USER_ID = os.environ.get("APPROVER_USER_ID")


# Slack Boltアプリを初期化
app = App(token=SLACK_BOT_TOKEN)


# ----------------------------------------------------
# ヘルパー関数 (API連携など)
# ----------------------------------------------------

def get_email_from_slack(user_id, client):
    """SlackのユーザーIDからメールアドレスを取得する"""
    try:
        result = client.users_info(user=user_id)
        return result["user"]["profile"]["email"]
    except Exception as e:
        logging.error(f"Slackのメールアドレス取得エラー: {e}")
        return None

def get_freee_employee_id_by_email(email):
    """メールアドレスからfreeeの従業員IDを取得する"""
    url = f"https://api.freee.co.jp/hr/api/v1/companies/{FREEEE_COMPANY_ID}/employees"
    headers = {
        "Authorization": f"Bearer {FREEEE_API_TOKEN}",
        "FREEEE-COMPANY-ID": str(FREEEE_COMPANY_ID),
    }
    params = {"email": email}
    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        employees = response.json().get("employees", [])
        if employees:
            return employees[0]["id"]
        return None
    except Exception as e:
        logging.error(f"freeeの従業員検索エラー: {e}")
        return None

def call_freee_time_clock(employee_id, clock_type):
    """freeeに打刻データを送信する"""
    url = f"https://api.freee.co.jp/hr/api/v1/employees/{employee_id}/time_clocks"
    headers = {
        "Authorization": f"Bearer {FREEEE_API_TOKEN}",
        "FREEEE-COMPANY-ID": str(FREEEE_COMPANY_ID),
        "Content-Type": "application/json"
    }
    data = {
        "company_id": int(FREEEE_COMPANY_ID),
        "type": clock_type,
        "base_date": datetime.date.today().isoformat(),
        "datetime": datetime.datetime.now().isoformat()
    }
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        return True
    except Exception as e:
        logging.error(f"freeeへの打刻API呼び出しエラー: {e}")
        return False

def get_freee_leave_types():
    """freeeから休暇種別の一覧を取得する"""
    url = f"https://api.freee.co.jp/hr/api/v1/companies/{FREEEE_COMPANY_ID}/work_record_templates"
    headers = {
        "Authorization": f"Bearer {FREEEE_API_TOKEN}",
        "FREEEE-COMPANY-ID": str(FREEEE_COMPANY_ID),
    }
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        templates = response.json()
        
        leave_types = [
            {"id": t["id"], "name": t["name"]}
            for t in templates if t.get("category") == "leave"
        ]
        return leave_types
    except Exception as e:
        logging.error(f"freeeの休暇種別取得エラー: {e}")
        return None

def add_event_to_google_calendar(summary, start_date, end_date):
    """Googleカレンダーに終日予定を追加する（アクセストークン使用）"""
    try:
        creds = Credentials(token=GOOGLE_ACCESS_TOKEN)
        service = build('calendar', 'v3', credentials=creds)

        end_date_for_api = (datetime.datetime.strptime(end_date, '%Y-%m-%d') + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        
        event = {
            'summary': summary,
            'start': {'date': start_date, 'timeZone': 'Asia/Tokyo'},
            'end': {'date': end_date_for_api, 'timeZone': 'Asia/Tokyo'},
        }
        service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        return True
    except HttpError as error:
        logging.error(f"Google Calendar API エラー: {error}")
        return False
    except Exception as e:
        logging.error(f"Googleカレンダーへの予定追加エラー: {e}")
        return False

# ----------------------------------------------------
# Slackコマンドのハンドラー
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
                                 {"text": {"type": "plain_text", "text": "🏠 在宅"}, "value": "在宅"},
                                 {"text": {"type": "plain_text", "text": "🏢 本社出社"}, "value": "本社出社"},
                                 {"text": {"type": "plain_text", "text": "💼 現場出社"}, "value": "現場出社"},
                                 {"text": {"type": "plain_text", "text": "✈️ 出張"}, "value": "出張"}]}}]}
    )

@app.command("/退勤")
def handle_clock_out_command(ack, body, client):
    ack()
    user_id = body["user_id"]
    email = get_email_from_slack(user_id, client)
    
    if not email:
        client.chat_postMessage(channel=user_id, text="エラー: Slackからメールアドレスを取得できませんでした。")
        return

    employee_id = get_freee_employee_id_by_email(email)
    if not employee_id:
        client.chat_postMessage(channel=user_id, text="エラー: freeeに従業員情報が見つかりませんでした。")
        return

    if call_freee_time_clock(employee_id, "clock_out"):
        client.chat_postMessage(channel=user_id, text="退勤打刻が完了しました。お疲れ様でした！")
    else:
        client.chat_postMessage(channel=user_id, text="エラー: freeeへの打刻処理に失敗しました。")

@app.command("/休暇申請")
def handle_leave_request_command(ack, body, client):
    ack()
    
    leave_types = get_freee_leave_types()
    
    if leave_types is None:
        client.chat_postMessage(channel=body["user_id"], text="エラー: freeeから休暇種別を取得できませんでした。")
        return

    options = [
        {"text": {"type": "plain_text", "text": leave["name"]}, "value": f"{leave['id']}:{leave['name']}"}
        for leave in leave_types
    ]
    
    try:
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal", "callback_id": "leave_request_view", "title": {"type": "plain_text", "text": "休暇申請"},
                "submit": {"type": "plain_text", "text": "申請"}, "blocks": [
                    {
                        "type": "input",
                        "block_id": "leave_type_block",
                        "label": {"type": "plain_text", "text": "休暇種別"},
                        "element": {
                            "type": "static_select",
                            "action_id": "leave_type_select",
                            "placeholder": {"type": "plain_text", "text": "休暇種別を選択"},
                            "options": options
                        }
                    },
                    {"type": "input", "block_id": "start_date_block", "label": {"type": "plain_text", "text": "開始日"},
                     "element": {"type": "datepicker", "action_id": "start_date_picker", "initial_date": datetime.datetime.now().strftime('%Y-%m-%d')}},
                    {"type": "input", "block_id": "end_date_block", "label": {"type": "plain_text", "text": "終了日"},
                     "element": {"type": "datepicker", "action_id": "end_date_picker", "initial_date": datetime.datetime.now().strftime('%Y-%m-%d')}},
                    {"type": "input", "block_id": "reason_block", "label": {"type": "plain_text", "text": "詳細理由（任意）"},
                     "element": {"type": "plain_text_input", "action_id": "reason_input", "placeholder": {"type": "plain_text", "text": "例：通院のため"}, "multiline": True},
                     "optional": True
                    },
                ]}
        )
    except Exception as e:
        logging.error(f"モーダル表示エラー: {e}")


# ----------------------------------------------------
# Slackモーダル送信のハンドラー
# ----------------------------------------------------

@app.view("clock_in_modal")
def handle_clock_in_submission(ack, body, client, view):
    ack()
    user_id = body["user"]["id"]
    location = view["state"]["values"]["location_block"]["location_select"]["selected_option"]["value"]
    
    email = get_email_from_slack(user_id, client)
    if not email:
        client.chat_postMessage(channel=user_id, text="エラー: Slackからメールアドレスを取得できませんでした。")
        return
        
    employee_id = get_freee_employee_id_by_email(email)
    if not employee_id:
        client.chat_postMessage(channel=user_id, text="エラー: freeeに従業員情報が見つかりませんでした。")
        return

    if call_freee_time_clock(employee_id, "clock_in"):
        client.chat_postMessage(channel=user_id, text=f"出勤打刻が完了しました。（勤務形態: {location}）\n今日も一日頑張りましょう！")
    else:
        client.chat_postMessage(channel=user_id, text="エラー: freeeへの打刻処理に失敗しました。")

@app.view("leave_request_view")
def handle_leave_request_submission(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    user_name = body["user"]["name"]
    values = body["view"]["state"]["values"]

    selected_option = values["leave_type_block"]["leave_type_select"]["selected_option"]["value"]
    leave_type_id, leave_type_name = selected_option.split(':', 1)

    start_date = values["start_date_block"]["start_date_picker"]["selected_date"]
    end_date = values["end_date_block"]["end_date_picker"]["selected_date"]
    
    summary = f"休暇({leave_type_name})：{user_name}"

    if add_event_to_google_calendar(summary, start_date, end_date):
        client.chat_postMessage(channel=user_id, text=f"休暇申請を受け付け、カレンダーに登録しました。\n種別：{leave_type_name}\n期間：{start_date} ~ {end_date}")
    else:
        client.chat_postMessage(channel=user_id, text="カレンダーへの登録に失敗しました。アクセストークンが古い可能性があります。管理者に連絡してください。")

# ----------------------------------------------------
# アプリケーションの起動
# ----------------------------------------------------
if __name__ == "__main__":
    logging.info("🤖 WorkStamper is running!")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()