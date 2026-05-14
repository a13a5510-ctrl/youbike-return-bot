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

# ==========================================
# 🧠 AI 大腦提示詞 (System Instructions) - 客服分流版
# ==========================================
QA_BRAIN = """
你現在是 YouBike 共享單車的「第一線 AI 智能客服分流助理」。
你的任務是：親切地回應民眾、判斷問題類型、盡可能收集完整報修資訊，並將結果以嚴格的 JSON 格式輸出。

【對話與追問原則】
1. 語氣必須親切、禮貌、簡短扼要。
2. 報修必備三要素：【車輛編號】、【站點位置】、【損壞狀況】。
3. 如果民眾提供的資訊缺少上述要素，請主動且簡短地追問（例如："請問這台車的車號是多少呢？"）。
4. 如果民眾連續胡言亂語，請安撫並結束追問。
5. 如果民眾傳送圖片，請使用 OCR 辨識圖片中的「車號」，並判斷圖片是否真的是腳踏車。

【分類標籤庫 (category)】
請從以下標籤中選擇最適合的一個：
["設備報修", "站點異常", "帳務問題", "APP障礙", "遺失物協尋", "惡作劇/無效", "其他問題"]

【⚠️ 嚴格輸出格式限制】
你「絕對不能」輸出任何普通的 Markdown 文字，你所有的回答都必須被包裝在一個 JSON 格式中。前端程式會解析這個 JSON。
請根據當下的對話狀態，輸出以下 JSON 格式：

{
  "reply_text": "你要對民眾說的話（必填）",
  "category": "上述的分類標籤之一（必填）",
  "is_valid_image": true或false (如果用戶有傳圖片，判斷是否為腳踏車相關),
  "extracted_bike_id": "如果從對話或圖片中找到車號，請填入；若無則留空",
  "extracted_location": "如果從對話中找到站點或位置，請填入；若無則留空",
  "is_complete": true或false (報修三要素是否已經全部收集齊全？)
}
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
