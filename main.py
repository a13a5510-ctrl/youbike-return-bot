import os
import json
import gspread
import google.generativeai as genai
from datetime import datetime
from flask import Flask, request, abort
from google.oauth2.service_account import Credentials
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, 
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

# --- 1. 環境變數 ---
LINE_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
AI_KEY = os.getenv('GEMINI_API_KEY')
SHEET_ID = os.getenv('SPREADSHEET_ID')
# GOOGLE_SHEETS_JSON 儲存完整的 Service Account JSON 內容
SHEETS_JSON = os.getenv('GOOGLE_SHEETS_JSON')

configuration = Configuration(access_token=LINE_TOKEN)
handler = WebhookHandler(LINE_SECRET)
genai.configure(api_key=AI_KEY)

# --- 2. Google Sheets 記帳功能 ---
def log_to_sheet(item, amount, category, note=""):
    try:
        scope = ['https://www.googleapis.com/auth/spreadsheets']
        creds_info = json.loads(SHEETS_JSON)
        creds = Credentials.from_service_account_info(creds_info, scopes=scope)
        client = gspread.authorize(creds)
        
        # 開啟試算表並選取第一個工作表
        sheet = client.open_by_key(SHEET_ID).sheet1
        
        # 格式：日期 | 項目 | 金額 | 分類 | 備註
        now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        sheet.append_row([now, item, amount, category, note])
        return True
    except Exception as e:
        print(f"❌ 雲端記帳失敗: {e}")
        return False

# --- 3. 系統指令 (強化數據提取) ---
ACCOUNTANT_BRAIN = """
你是一位資深會計師。請幫忙結算帳目。
格式要求：
1. 先給出親切且專業的結算報告。
2. 結尾請務必加上一行特定的隱藏標籤，格式如下（僅限一組最重要的數據）：
DATABASE_UPDATE: {"item": "項目簡稱", "amount": 總金額數字, "category": "分類"}
分類請從中選擇：食、衣、住、行、育、樂、其他。
"""

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_msg = event.message.text
    print(f"📡 收到帳務訊息: {user_msg}")

    try:
        # 使用 2026 年穩定的模型節點
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            system_instruction=ACCOUNTANT_BRAIN
        )
        response = model.generate_content(user_msg)
        full_reply = response.text

        # 🕵️ 數據抓取邏輯
        if "DATABASE_UPDATE:" in full_reply:
            try:
                # 提取 JSON 部分
                json_str = full_reply.split("DATABASE_UPDATE:")[1].strip()
                data = json.loads(json_str)
                
                # 執行寫入 Google Sheets
                success = log_to_sheet(data['item'], data['amount'], data['category'], "LINE 自動入帳")
                
                # 清除回覆中的標籤，不讓用戶看到醜醜的代碼
                display_reply = full_reply.split("DATABASE_UPDATE:")[0].strip()
                if success:
                    display_reply += "\n\n✅ [會計師備註] 此筆帳目已同步至雲端帳本。"
            except:
                display_reply = full_reply # 解析失敗則回傳原文
        else:
            display_reply = full_reply

    except Exception as e:
        display_reply = f"❌ 會計師暫時無法運算：{str(e)[:50]}"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=display_reply)]
            )
        )
# --- 這裡接在原本 handle_message 函數的結束之後 ---

if __name__ == "__main__":
    # 這是最重要的啟動指令，確保它監聽在 Render 指定的 Port 並對外開放 (0.0.0.0)
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
