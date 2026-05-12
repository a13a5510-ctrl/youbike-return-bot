import os
import json
import firebase_admin
from firebase_admin import credentials, firestore, storage
from datetime import datetime
from flask import Flask, request, abort
import google.generativeai as genai

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent

app = Flask(__name__)

# --- 1. 環境變數 ---
LINE_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
AI_KEY = os.getenv('GEMINI_API_KEY')
# 注意：這裡改成讀取 FIREBASE 的環境變數
FIREBASE_JSON_STR = os.getenv('FIREBASE_CREDENTIALS') 

configuration = Configuration(access_token=LINE_TOKEN)
handler = WebhookHandler(LINE_SECRET)
genai.configure(api_key=AI_KEY)

# --- 2. 初始化 Firebase ---
try:
    firebase_creds = json.loads(FIREBASE_JSON_STR)
    cred = credentials.Certificate(firebase_creds)
    # 這裡填寫你剛剛取得的 Storage Bucket 網址
    firebase_admin.initialize_app(cred, {
        'storageBucket': 'youbike-return-bot.firebasestorage.app'
    })
    db = firestore.client()
    bucket = storage.bucket()
    print("✅ Firebase 初始化成功！")
except Exception as e:
    print(f"❌ Firebase 初始化失敗: {e}")

# --- 3. 系統指令 (AI 大腦) ---
QA_BRAIN = """
你現在是「程式語言改善生活」的專屬智慧助理。
你的任務是針對使用者的問題，給出精確、實用且語氣友善的回答。
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

# --- 4. 處理文字訊息 ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_msg = event.message.text
    print(f"📡 收到文字訊息: {user_msg}")

    try:
        # 呼叫 Gemini
        model = genai.GenerativeModel(model_name="gemini-2.5-flash", system_instruction=QA_BRAIN)
        response = model.generate_content(user_msg)
        ai_reply = response.text

        # 存入 Firestore 資料庫
        doc_ref = db.collection('chats').document()
        doc_ref.set({
            'timestamp': datetime.now(),
            'type': 'text',
            'user_say': user_msg,
            'ai_reply': ai_reply,
            'image_url': None
        })

    except Exception as e:
        ai_reply = f"❌ 助理大腦當機啦：{str(e)[:50]}"

    reply_to_line(event.reply_token, ai_reply)

# --- 5. 處理圖片訊息 ---
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    message_id = event.message.id
    print(f"📸 收到圖片訊息 ID: {message_id}")

    try:
        # 1. 從 LINE 下載圖片
        with ApiClient(configuration) as api_client:
            line_bot_blob_api = MessagingApiBlob(api_client)
            image_content = line_bot_blob_api.get_message_content(message_id)
        
        # 2. 上傳到 Firebase Storage
        blob = bucket.blob(f"line_images/{message_id}.jpg")
        blob.upload_from_string(image_content, content_type='image/jpeg')
        blob.make_public() # 讓圖片可以對外公開顯示
        image_url = blob.public_url

        # 3. 回覆用戶 (若要 AI 辨識圖片，需修改此處邏輯，目前先簡單回覆)
        ai_reply = "✅ 我已經把你的照片存到資料庫囉！可以去網頁上查看。"

        # 4. 存入 Firestore 資料庫
        doc_ref = db.collection('chats').document()
        doc_ref.set({
            'timestamp': datetime.now(),
            'type': 'image',
            'user_say': '傳送了一張圖片',
            'ai_reply': ai_reply,
            'image_url': image_url
        })

    except Exception as e:
        ai_reply = f"❌ 圖片處理失敗：{str(e)[:50]}"

    reply_to_line(event.reply_token, ai_reply)

# --- 共用回覆函數 ---
def reply_to_line(reply_token, text):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
