import os
import json
import time
import urllib.request
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
# 📡 抓取 YouBike 官方站點資料 (用於自動校正與經緯度定位)
# ==========================================
YOUBIKE_STATIONS = []
try:
    url = "https://tcgbusfs.blob.core.windows.net/dotapp/youbike/v2/youbike_immediate.json"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response:
        YOUBIKE_STATIONS = json.loads(response.read().decode())
        print(f"✅ 成功載入 {len(YOUBIKE_STATIONS)} 個微笑單車站點！")
except Exception as e:
    print(f"❌ 載入站點失敗: {e}")

# ==========================================
# 🧠 AI 大腦提示詞 - 完美法規與防呆版
# ==========================================
QA_BRAIN = """
你現在是 YouBike 共享單車的「第一線 AI 智能客服分流助理」。
你的任務是：親切地回應民眾、收集完整報修資訊，並將結果以嚴格的 JSON 格式輸出。

【對話與服務 SOP】
1. 首次接觸與個資同意 (最高優先)：收到新報修時，必須先發送聲明：「您好！為利後續聯繫，需先取得您的同意蒐集聯絡電話，請問您的聯絡電話是？」若民眾明示拒絕，委婉告知無法受理。
2. 報修必備要素：取得電話後，再開始收集【車輛編號】、【站點位置】、【損壞狀況】。
3. 複數車輛與場站異常：若民眾表示「整排車壞掉」或多台車，直接判定為「站點異常」，無需強迫提供單一車號。
4. 資訊更正機制：若民眾表示打錯了，請理解並更新為正確資訊。
5. 語言與錯字規範：若使用注音文或大量錯字導致無法辨識，委婉請對方使用標準中文。
6. 緊急狀況：遇「車禍、起火」等危急字眼，加入「緊急狀況請立即撥打 119 或 0800 專線」，並記錄。

【分類標籤庫 (category)】
["設備報修", "站點異常", "帳務問題", "APP障礙", "遺失物協尋", "惡作劇/無效", "緊急通報", "其他問題"]

【⚠️ 嚴格輸出格式限制】
你所有的回答都必須被包裝在 JSON 格式中，絕對不能輸出 Markdown 文字。
{
  "reply_text": "你要對民眾說的話（必填）",
  "category": "上述的分類標籤之一（必填）",
  "is_valid_image": true或false,
  "extracted_phone": "找到的民眾電話(若無則留空)",
  "extracted_bike_id": "找到的車號(若無則留空)",
  "extracted_location": "找到的站點位置(若無則留空)",
  "is_complete": true或false
}
"""

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="gemini-2.5-flash", system_instruction=QA_BRAIN)

# 初始化 Firebase
db = None
bucket = None
if FIREBASE_CREDENTIALS:
    try:
        cred_dict = json.loads(FIREBASE_CREDENTIALS)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {
            'storageBucket': "youbike-return-bot.firebasestorage.app"
        })
        db = firestore.client()
        bucket = storage.bucket()
        print("✅ Firebase 初始化成功！")
    except Exception as e:
        print(f"❌ 初始化失敗: {e}")

USER_SESSIONS = {}

def get_session(user_id):
    now = time.time()
    session = USER_SESSIONS.get(user_id, {
        "history": [], "last_active": now, "bad_image_count": 0, 
        "frozen_until": 0, "doc_id": None, "full_message": ""
    })
    if now - session["last_active"] > 600: # 延長為 10 分鐘冷凍與生命週期
        session["history"] = []; session["doc_id"] = None; session["full_message"] = ""
    session["last_active"] = now
    USER_SESSIONS[user_id] = session
    return session

def clean_json_string(raw_text):
    start = raw_text.find('{'); end = raw_text.rfind('}')
    return raw_text[start:end+1] if start != -1 and end != -1 else "{}"

def enrich_location_data(report_data, loc_str):
    """利用 YouBike API 進行模糊比對，校正站名並取得 GPS 座標 (終極防禦版)"""
    report_data['location'] = loc_str
    if len(loc_str) >= 2:
        for st in YOUBIKE_STATIONS:
            # 安全取得站名，若該站資料損毀則跳過
            st_name = st.get('sna', '')
            if not st_name:
                continue
                
            sna_clean = st_name.replace('YouBike2.0_', '')
            
            # 若民眾說的地點有比中站點名稱
            if loc_str in sna_clean or sna_clean in loc_str:
                report_data['location'] = sna_clean # 校正官方站名
                
                # 【金鐘罩取值法】安全獲取經緯度，支援多種常見命名，找不到也不會當機！
                lat = st.get('lat') or st.get('latitude')
                lng = st.get('lng') or st.get('longitude') or st.get('lon')
                
                if lat and lng:
                    report_data['lat'] = float(lat)
                    report_data['lng'] = float(lng)
                break

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_id = event.source.user_id; user_msg = event.message.text
    session = get_session(user_id)
    if time.time() < session["frozen_until"]: return
    session["history"].append({"role": "user", "parts": [user_msg]})
    session["full_message"] = f"{session['full_message']} ｜ {user_msg}".strip(" ｜ ")
    try:
        chat = model.start_chat(history=session["history"][:-1])
        response = chat.send_message(user_msg)
        session["history"].append({"role": "model", "parts": [response.text]})
        result = json.loads(clean_json_string(response.text))
        reply_text = result.get("reply_text", "處理中...")
        
        if db:
            report_data = {
                'user_id': user_id, 'message': session["full_message"],
                'reply': reply_text, 'category': result.get("category", "未分類"),
                'is_complete': result.get("is_complete", False),
                'status': 'pending', 'timestamp': firestore.SERVER_TIMESTAMP
            }
            if result.get("extracted_phone"): report_data['phone'] = result.get("extracted_phone")
            if result.get("extracted_bike_id"): report_data['bike_id'] = result.get("extracted_bike_id")
            if result.get("extracted_location"):
                enrich_location_data(report_data, result.get("extracted_location"))

            if not session["doc_id"]:
                new_ref = db.collection('user_reports').document()
                new_ref.set(report_data); session["doc_id"] = new_ref.id
            else:
                db.collection('user_reports').document(session["doc_id"]).set(report_data, merge=True)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    except Exception as e:
        print(f"❌ 錯誤: {e}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="系統繁忙，請稍後再試。"))

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id; session = get_session(user_id)
    if time.time() < session["frozen_until"]: return
    session["full_message"] = f"{session['full_message']} ｜ [傳送了圖片]".strip(" ｜ ")
    
    msg_content = line_bot_api.get_message_content(event.message.id)
    image_data = b"".join(msg_content.iter_content())
    
    try:
        img_url = ""
        if bucket:
            file_path = f"reports/{user_id}_{int(time.time())}.jpg"
            blob = bucket.blob(file_path)
            blob.upload_from_string(image_data, content_type='image/jpeg')
            blob.make_public()
            img_url = blob.public_url

        image_part = {"mime_type": "image/jpeg", "data": image_data}
        chat = model.start_chat(history=session["history"])
        response = chat.send_message(["解析圖片中是否有車號與腳踏車，嚴格以JSON回傳。", image_part])
        session["history"].append({"role": "model", "parts": [response.text]})
        result = json.loads(clean_json_string(response.text))
        
        reply_text = result.get("reply_text", "收到照片。")
        if not result.get("is_valid_image", True):
            session["bad_image_count"] += 1
            if session["bad_image_count"] >= 2:
                session["frozen_until"] = time.time() + 600 # 冷凍 10 分鐘
                session["bad_image_count"] = 0
                reply_text = "⚠️ 偵測到連續無關圖片，為避免資源浪費，暫停報修 10 分鐘。"
        else:
            session["bad_image_count"] = 0
            
        if db:
            report_data = {
                'user_id': user_id, 'message': session["full_message"],
                'reply': reply_text, 'category': result.get("category", "未分類"),
                'is_complete': result.get("is_complete", False),
                'status': 'pending', 'timestamp': firestore.SERVER_TIMESTAMP,
                'image_urls': firestore.ArrayUnion([img_url]) if img_url else []
            }
            if result.get("extracted_phone"): report_data['phone'] = result.get("extracted_phone")
            if result.get("extracted_bike_id"): report_data['bike_id'] = result.get("extracted_bike_id")
            if result.get("extracted_location"):
                enrich_location_data(report_data, result.get("extracted_location"))

            if not session["doc_id"]:
                new_ref = db.collection('user_reports').document()
                new_ref.set(report_data); session["doc_id"] = new_ref.id
            else:
                db.collection('user_reports').document(session["doc_id"]).set(report_data, merge=True)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    except Exception as e:
        print(f"❌ 錯誤: {e}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="圖片處理失敗。"))

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
