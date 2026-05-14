import os
import json
import time
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, firestore, storage

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
# 🧠 AI 大腦提示詞 - 完美法規與防呆版
# ==========================================
QA_BRAIN = """
你現在是 YouBike 共享單車的「第一線 AI 智能客服分流助理」。
你的任務是：親切地回應民眾、收集完整報修資訊，並將結果以嚴格的 JSON 格式輸出。

【對話與服務 SOP】
1. 首次接觸與個資同意 (最高優先級)：收到新報修時，必須先發送簡短的聲明：「您好！為利後續聯繫，需先取得您的同意蒐集聯絡電話，請問您的聯絡電話是？」若民眾明示拒絕提供，請委婉告知無法受理並結束對話。
2. 報修必備要素：取得電話後，再開始收集【車輛編號】、【站點位置】、【損壞狀況】。
3. 複數車輛與場站異常：若民眾表示「整排車壞掉」或提供多台車號，請直接判定並回覆為「站點異常」，無需強迫提供所有單一車號。
4. 資訊更正機制：若民眾表示剛剛打錯了（如更正車號或地點），請理解並在輸出的 JSON 中更新為正確資訊。
5. 語言與錯字規範：若民眾使用大量注音文、方言諧音或錯字導致無法辨識，請委婉請對方使用「標準中文」以便系統記錄。
6. 緊急狀況：遇到「車禍、起火」等危急字眼，請在回覆中加入「緊急狀況請立即撥打 119 或 0800-xxx-xxx 專線」，並依然記錄此事件。

【分類標籤庫 (category)】
請從以下標籤中選擇最適合的一個：
["設備報修", "站點異常", "帳務問題", "APP障礙", "遺失物協尋", "惡作劇/無效", "緊急通報", "其他問題"]

【⚠️ 嚴格輸出格式限制】
你所有的回答都必須被包裝在一個 JSON 格式中，絕對不能輸出普通的 Markdown 文字。
格式如下：
{
  "reply_text": "你要對民眾說的話（必填）",
  "category": "上述的分類標籤之一（必填）",
  "is_valid_image": true或false,
  "extracted_phone": "找到的民眾電話(若無則留空)",
  "extracted_bike_id": "找到的車號(若無則留空)",
  "extracted_location": "找到的站點位置(若無則留空)",
  "is_complete": true或false (電話與三要素是否皆已收集齊全)
}
"""

# 初始化 Gemini (使用 2.5-flash 大腦)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="gemini-2.5-flash", system_instruction=QA_BRAIN)

# 初始化 Firebase (同時啟動 Firestore 與 Storage)
db = None
bucket = None
if FIREBASE_CREDENTIALS:
    try:
        cred_dict = json.loads(FIREBASE_CREDENTIALS)
        cred = credentials.Certificate(cred_dict)
        # 換成你專屬的最新版 Storage 名稱
        firebase_admin.initialize_app(cred, {
            'storageBucket': "youbike-return-bot.firebasestorage.app"
        })
        db = firestore.client()
        bucket = storage.bucket()
        print("✅ Firebase Firestore & Storage 初始化成功！")
    except Exception as e:
        print(f"❌ 初始化失敗: {e}")
# ==========================================
# 🛡️ 記憶與防禦陣法 (Session & 單一單號追蹤)
# ==========================================
USER_SESSIONS = {}

def get_session(user_id):
    now = time.time()
    session = USER_SESSIONS.get(user_id, {
        "history": [], 
        "last_active": now, 
        "bad_image_count": 0, 
        "frozen_until": 0,
        "doc_id": None,        # 記錄資料庫的單號
        "full_message": ""     # 累積完整的對話紀錄
    })
    
    # 【生命週期結界】超過五分鐘 (300秒) 未回覆，清空記憶重新開單
    if now - session["last_active"] > 300:
        session["history"] = []
        session["doc_id"] = None
        session["full_message"] = ""
        
    session["last_active"] = now
    USER_SESSIONS[user_id] = session
    return session

def clean_json_string(raw_text):
    """極限防禦版：直接暴力擷取 {} 之間的內容，無視多餘廢話"""
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
    
    if time.time() < session["frozen_until"]:
        return

    session["history"].append({"role": "user", "parts": [user_msg]})
    
    # 累積對話內容 (讓長官看到完整對話)
    if session["full_message"]:
        session["full_message"] += f" ｜ {user_msg}"
    else:
        session["full_message"] = user_msg
    
    try:
        chat = model.start_chat(history=session["history"][:-1])
        response = chat.send_message(user_msg)
        session["history"].append({"role": "model", "parts": [response.text]})
        
        # 解析 JSON
        json_str = clean_json_string(response.text)
        result = json.loads(json_str)
        reply_text = result.get("reply_text", "收到回報，處理中。")
        
        # 寫入/更新 Firebase
        if db:
            report_data = {
                'user_id': user_id,
                'message': session["full_message"],
                'reply': reply_text,
                'category': result.get("category", "未分類"),
                'is_complete': result.get("is_complete", False),
                'status': 'pending',
                'timestamp': firestore.SERVER_TIMESTAMP
            }
            
            # 防呆機制：只在有抓到新資料時才更新
            if result.get("extracted_phone"):
                report_data['phone'] = result.get("extracted_phone") # 新增這行存電話
            if result.get("extracted_bike_id"):
                report_data['bike_id'] = result.get("extracted_bike_id")
            if result.get("extracted_location"):
                report_data['location'] = result.get("extracted_location")

            if not session["doc_id"]:
                # 第一次發言：新增單號
                new_ref = db.collection('user_reports').document()
                new_ref.set(report_data)
                session["doc_id"] = new_ref.id
            else:
                # 繼續對話：更新舊單 (merge=True 保留其他欄位，如 image_urls)
                db.collection('user_reports').document(session["doc_id"]).set(report_data, merge=True)
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        
    except Exception as e:
        print(f"❌ 錯誤: {e}")
        fallback_msg = "系統目前線路壅塞，請稍後再試。如為緊急維修請致電 0800 客服專線。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=fallback_msg))

# ==========================================
# 📸 圖片訊息處理 (存證大法核心)
# ==========================================
@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id
    session = get_session(user_id)
    
    if time.time() < session["frozen_until"]:
        return

    # 累積對話紀錄
    if session["full_message"]:
        session["full_message"] += " ｜ [傳送了圖片]"
    else:
        session["full_message"] = "[傳送了圖片]"

    # 下載圖片二進位檔
    msg_content = line_bot_api.get_message_content(event.message.id)
    image_data = b"".join(msg_content.iter_content())

    try:
        # 【魔法一：上傳圖片到 Firebase Storage】
        img_url = ""
        if bucket:
            file_path = f"reports/{user_id}_{int(time.time())}.jpg"
            blob = bucket.blob(file_path)
            blob.upload_from_string(image_data, content_type='image/jpeg')
            blob.make_public()  # 開放權限讓網頁能讀取
            img_url = blob.public_url

        # 【魔法二：將圖片拿給 Gemini 看】
        image_part = {"mime_type": "image/jpeg", "data": image_data}
        prompt = "用戶上傳圖片，請解析是否為腳踏車，並找出是否有車號。請嚴格以JSON回傳。"
        
        chat = model.start_chat(history=session["history"])
        response = chat.send_message([prompt, image_part])
        session["history"].append({"role": "model", "parts": [response.text]})
        
        result = json.loads(clean_json_string(response.text))
        reply_text = result.get("reply_text", "已收到您的圖片！")
        is_valid = result.get("is_valid_image", True)
        
        # 惡意流量防禦
        if not is_valid:
            session["bad_image_count"] += 1
            if session["bad_image_count"] >= 2:
                session["frozen_until"] = time.time() + 300
                session["bad_image_count"] = 0
                reply_text = "⚠️ 系統偵測到您連續上傳與報修無關的圖片。報修功能將暫停 5 分鐘。"
        else:
            session["bad_image_count"] = 0
            
        # 【魔法三：寫入資料庫並綁定網址】
        if db:
            report_data = {
                'user_id': user_id,
                'message': session["full_message"],
                'reply': reply_text,
                'category': result.get("category", "未分類"),
                'is_complete': result.get("is_complete", False),
                'status': 'pending',
                'timestamp': firestore.SERVER_TIMESTAMP
            }
            
            # 使用 ArrayUnion 將圖片網址加入陣列 (不會洗掉舊圖片)
            if img_url:
                report_data['image_urls'] = firestore.ArrayUnion([img_url])
                
            if result.get("extracted_bike_id"):
                report_data['bike_id'] = result.get("extracted_bike_id")
            if result.get("extracted_location"):
                report_data['location'] = result.get("extracted_location")

            if not session["doc_id"]:
                new_ref = db.collection('user_reports').document()
                new_ref.set(report_data)
                session["doc_id"] = new_ref.id
            else:
                db.collection('user_reports').document(session["doc_id"]).set(report_data, merge=True)
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        
    except Exception as e:
        print(f"❌ 錯誤: {e}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="圖片解析失敗，請確認圖片清晰或稍後再試。"))

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
