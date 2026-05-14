import os
import json
import time
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)

# ==========================================
# 🔑 金鑰與環境變數設定
# ==========================================
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
FIREBASE_CREDENTIALS = os.getenv('FIREBASE_CREDENTIALS')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ==========================================
# 🧠 AI 大腦提示詞 - 客服分流結構化版
# ==========================================
QA_BRAIN = """
你現在是 YouBike 共享單車的「第一線 AI 智能客服分流助理」。
你的任務是：親切地回應民眾、判斷問題類型、盡可能收集完整報修資訊，並將結果以嚴格的 JSON 格式輸出。

【對話與追問原則】
1. 語氣必須親切、禮貌、簡短扼要。
2. 報修必備三要素：【車輛編號】、【站點位置】、【損壞狀況】。
3. 如果民眾提供的資訊缺少上述要素，請主動且簡短地追問（例如："請問這台車的車號是多少呢？"）。
4. 如果民眾連續胡言亂語，請安撫並結束追問。
5. 如果民眾傳送圖片，請使用 OCR 辨識圖片中的「車號」，並判斷圖片是否真的是腳踏車相關。

【分類標籤庫 (category)】
請從以下標籤中選擇最適合的一個：
["設備報修", "站點異常", "帳務問題", "APP障礙", "遺失物協尋", "惡作劇/無效", "其他問題"]

【⚠️ 嚴格輸出格式限制】
你「絕對不能」輸出任何普通的 Markdown 文字，你所有的回答都必須被包裝在一個 JSON 格式中。
格式如下：
{
  "reply_text": "你要對民眾說的話（必填）",
  "category": "上述的分類標籤之一（必填）",
  "is_valid_image": true或false,
  "extracted_bike_id": "找到的車號(若無則留空)",
  "extracted_location": "找到的站點位置(若無則留空)",
  "is_complete": true或false
}
"""

# 初始化 Gemini (使用穩定的 1.5-flash)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="gemini-1.5-flash", system_instruction=QA_BRAIN)

# 初始化 Firebase
db = None
if FIREBASE_CREDENTIALS:
    try:
        cred_dict = json.loads(FIREBASE_CREDENTIALS)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("✅ Firebase 初始化成功！")
    except Exception as e:
        print(f"❌ Firebase 初始化失敗: {e}")

# ==========================================
# 🛡️ 記憶與防禦陣法 (Session & Spam Protection)
# ==========================================
USER_SESSIONS = {}

def get_session(user_id):
    now = time.time()
    session = USER_SESSIONS.get(user_id, {
        "history": [], 
        "last_active": now, 
        "bad_image_count": 0, 
        "frozen_until": 0
    })
    
    # 【生命週期結界】超過五分鐘 (300秒) 未回覆，清空記憶重新開始
    if now - session["last_active"] > 300:
        session["history"] = []
        
    session["last_active"] = now
    USER_SESSIONS[user_id] = session
    return session

def clean_json_string(raw_text):
    """極限防禦版：直接暴力擷取 {} 之間的內容，無視任何多餘的文字"""
    start = raw_text.find('{')
    end = raw_text.rfind('}')
    if start != -1 and end != -1:
        return raw_text[start:end+1]
    return "{}"

# ==========================================
# 🌐 Webhook 接收路由
# ==========================================
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# ==========================================
# 💬 文字訊息處理
# ==========================================
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_id = event.source.user_id
    user_msg = event.message.text
    session = get_session(user_id)
    
    # 檢查是否在冷凍期
    if time.time() < session["frozen_until"]:
        return

    session["history"].append({"role": "user", "parts": [user_msg]})
    
    try:
        chat = model.start_chat(history=session["history"][:-1])
        response = chat.send_message(user_msg)
        session["history"].append({"role": "model", "parts": [response.text]})
        
        # 解析 JSON
        json_str = clean_json_string(response.text)
        result = json.loads(json_str)
        reply_text = result.get("reply_text", "收到回報，處理中。")
        
        # 寫入 Firebase
        if db:
            db.collection('user_reports').add({
                'user_id': user_id,
                'message': user_msg,
                'reply': reply_text,
                'category': result.get("category", "未分類"),
                'bike_id': result.get("extracted_bike_id", ""),
                'location': result.get("extracted_location", ""),
                'is_complete': result.get("is_complete", False),
                'status': 'pending',
                'timestamp': firestore.SERVER_TIMESTAMP
            })
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        
    except Exception as e:
        print(f"❌ 錯誤: {e}")
        fallback_msg = "系統目前線路壅塞，請稍後再試。如為緊急維修請致電 0800 客服專線。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=fallback_msg))

# ==========================================
# 📸 圖片訊息處理
# ==========================================
@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id
    session = get_session(user_id)
    
    # 檢查是否在冷凍期
    if time.time() < session["frozen_until"]:
        return

    message_content = line_bot_api.get_message_content(event.message.id)
    image_data = b"".join(message_content.iter_content())
        
    image_part = {"mime_type": "image/jpeg", "data": image_data}

    try:
        prompt = "用戶上傳圖片，請解析是否為腳踏車，並找出是否有車號。請嚴格以JSON回傳。"
        chat = model.start_chat(history=session["history"])
        response = chat.send_message([prompt, image_part])
        session["history"].append({"role": "model", "parts": [response.text]})
        
        json_str = clean_json_string(response.text)
        result = json.loads(json_str)
        
        reply_text = result.get("reply_text", "已收到圖片！")
        is_valid = result.get("is_valid_image", True)
        
        # 【惡意流量防禦】連續兩次無關圖片即冷凍
        if not is_valid:
            session["bad_image_count"] += 1
            if session["bad_image_count"] >= 2:
                session["frozen_until"] = time.time() + 300
                session["bad_image_count"] = 0
                reply_text = "⚠️ 偵測到您連續上傳無關圖片，報修功能將暫停 5 分鐘。"
        else:
            session["bad_image_count"] = 0
            
        if db:
            db.collection('user_reports').add({
                'user_id': user_id,
                'message': "[用戶上傳了圖片]",
                'reply': reply_text,
                'category': result.get("category", "未分類"),
                'bike_id': result.get("extracted_bike_id", ""),
                'is_complete': result.get("is_complete", False),
                'status': 'pending',
                'timestamp': firestore.SERVER_TIMESTAMP
            })
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        
    except Exception as e:
        print(f"❌ 錯誤: {e}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="圖片解析失敗，請重傳或稍後再試。"))

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
