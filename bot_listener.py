import sys
import os
import re
import math
import traceback
import json
import base64
import queue
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, Blueprint, request, abort, jsonify, render_template
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, FlexSendMessage,
    BubbleContainer, BoxComponent, ButtonComponent, TextComponent, URIAction,
    UnsendEvent
)
import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler

bot_bp = Blueprint('bot_listener', __name__)

# ==================== 設定區 ====================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'CUe1Avu/wrK/rZW/k8BQ9GMIGYQBrP9K4i2e1iDJ3W7DJ0PFBcbOqye5uaoIBCA4gKQy1yyyw1P/t2PVOXHD7z7qD8demfUs/1cy3TIuT5THP0qD+nVxSJ/95kwtgrseHl9FRdvnwJlrHx4srmLSCQdB04t89/1O/w1cDnyilFU=')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', 'a3defb6dbcd3d0743cde1f0c0e100d95')
SHEET_NAME = "調度室派單管理表"
LIFF_ID = "2011777708-58qvPNLe"

BLOCKED_USER_IDS = set([])
BLOCKED_KEYWORDS = ["測試灌水", "廣告文字"]
# ================================================

scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
google_creds_raw = os.environ.get("GOOGLE_CREDENTIALS")

creds = None
gs_client = None

if google_creds_raw and google_creds_raw.strip():
    raw_str = google_creds_raw.strip()
    creds_info = None
    try:
        missing_padding = len(raw_str) % 4
        if missing_padding:
            raw_str += '=' * (4 - missing_padding)
        decoded_bytes = base64.b64decode(raw_str)
        json_str = decoded_bytes.decode("utf-8")
        creds_info = json.loads(json_str)
        print("✅ 成功將 GOOGLE_CREDENTIALS 解碼 Base64 並解析為 JSON")
    except Exception:
        pass

    if not creds_info:
        try:
            creds_info = json.loads(raw_str)
            print("✅ 成功將 GOOGLE_CREDENTIALS 直接解析為 JSON")
        except Exception as e:
            print(f"❌ 解析失敗: {e}")

    if creds_info:
        try:
            creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        except Exception as e:
            print(f"❌ 從憑證資訊建構 Credentials 失敗: {e}")

if not creds:
    if os.path.exists("credentials.json"):
        try:
            creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
            print("✅ 成功讀取本地 credentials.json 檔案")
        except Exception as e:
            print(f"❌ 讀取本地 credentials.json 失敗: {e}")

if creds:
    try:
        gs_client = gspread.authorize(creds)
        print("🎉 Google Sheet 授權認證成功！")
    except Exception as e:
        print(f"❌ 初始化 Google Sheet 失敗: {e}")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

sheet_data = [] 
msg_id_to_row = {}
write_queue = queue.Queue() # ⚡ 非同步寫入佇列，解決卡頓問題
sheet_lock = threading.Lock()

def get_sheet():
    if not gs_client:
        raise Exception("Google Sheet 用戶端未正確認證！")
    return gs_client.open(SHEET_NAME).sheet1

def init_sheet_data():
    global sheet_data
    if not gs_client:
        print("⚠️ 無法載入 Google Sheet：gs_client 未初始化！")
        return
    try:
        sheet = get_sheet()
        raw_data = sheet.get_all_values()
        formatted_data = []
        for row in raw_data:
            row_copy = list(row)
            while len(row_copy) < 6:
                row_copy.append("")
            formatted_data.append(row_copy)
        with sheet_lock:
            sheet_data = formatted_data
        print(f"⚡ [系統初始化] 成功載入 {len(sheet_data)} 列資料至記憶體！")
    except Exception as e:
        print("❌ 初始化 Google Sheet 失敗:", str(e))

def background_writer():
    """ 背景執行緒：依序安全寫入 Google Sheet，絕不卡住主線程 """
    print("🚀 [背景寫入執行緒] 已啟動運行")
    while True:
        try:
            task = write_queue.get()
            if task is None:
                break
            
            action, payload = task
            sheet = get_sheet()
            
            if action == "append":
                sheet.append_row(payload)
                print(f"☁️ [背景寫入成功] 新增單號: {payload[0][:20]}...")
            elif action == "update_cell":
                row, col, value = payload
                sheet.update_cell(row, col, value)
                print(f"☁️ [背景寫入成功] 第 {row} 列 Col {col} -> {value}")
            
            write_queue.task_done()
            time.sleep(1.0) # 控速保護，避免觸發 Google API 429 頻率限制
        except Exception as e:
            print(f"❌ [背景寫入異常]: {e}")
            time.sleep(2.0)

def parse_booking_time(text):
    now = datetime.now()
    match = re.search(r'(\d{1,2})[:：點\.](\d{2})?', text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            booking_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if booking_dt < now - timedelta(minutes=30):
                booking_dt += timedelta(days=1)
            return booking_dt.strftime("%Y-%m-%d %H:%M")
    return None

def check_reservation_orders():
    now = datetime.now()
    with sheet_lock:
        for idx in range(1, len(sheet_data)):
            row = sheet_data[idx]
            sheet_row_num = idx + 1 
            if len(row) >= 6 and row[5] == "預約單" and row[1] in ["未派出", "預約等待中"]:
                booking_time_str = row[4]
                if not booking_time_str:
                    continue
                try:
                    booking_dt = datetime.strptime(booking_time_str, "%Y-%m-%d %H:%M")
                    time_diff_minutes = (booking_dt - now).total_seconds() / 60.0

                    if time_diff_minutes <= 15:
                        if sheet_data[idx][1] != "未派出" or sheet_data[idx][5] != "即時":
                            sheet_data[idx][1] = "未派出"
                            sheet_data[idx][5] = "即時"
                            write_queue.put(("update_cell", (sheet_row_num, 2, "未派出")))
                            write_queue.put(("update_cell", (sheet_row_num, 6, "即時")))
                    elif time_diff_minutes <= 60:
                        if sheet_data[idx][1] != "未派出":
                            sheet_data[idx][1] = "未派出"
                            write_queue.put(("update_cell", (sheet_row_num, 2, "未派出")))
                except Exception:
                    pass

def init_bot_listener():
    init_sheet_data()
    # 啟動背景寫入執行緒
    threading.Thread(target=background_writer, daemon=True).start()
    # 啟動定時排程器
    try:
        scheduler = BackgroundScheduler()
        scheduler.add_job(func=check_reservation_orders, trigger="interval", seconds=60)
        scheduler.start()
        print("⏰ [排程器啟動成功]")
    except Exception as e:
        print(f"⚠️ 排程器啟動警告: {e}")

def calculate_fare(km, mins):
    base_fare = 80
    time_fare = mins * 3
    if km <= 20:
        dist_fare = km * 15
    else:
        dist_fare = (20 * 15) + ((km - 20) * (15 + 10))
    total_fare = math.ceil(base_fare + dist_fare + time_fare)
    return (
        f"🚖 【車資試算結果】\n"
        f"-------------------\n"
        f"📍 里程：{km:.1f} 公里\n"
        f"⏱️ 時間：{mins} 分鐘\n"
        f"-------------------\n"
        f"• 起步基本費：${base_fare}\n"
        f"• 里程計價費：${math.ceil(dist_fare)}\n"
        f"• 時間計時費：${math.ceil(time_fare)}\n"
        f"-------------------\n"
        f"💰 預估總車資：${total_fare} 元"
    )

def is_valid_driver_report(text):
    lines = [line.strip() for line in text.strip().split('\n') if line.strip()]
    if len(lines) not in [3, 4]:
        return False
    line_first = lines[0]
    line_car = lines[-2]
    line_time = lines[-1]
    if not line_first.startswith("#"):
        return False
    color_keywords = ["白", "黑", "銀", "灰", "紅", "藍", "黃", "綠", "金", "紫", "棕"]
    has_car_info = re.search(r'\d+', line_car) and any(c in line_car for c in color_keywords)
    has_time_info = bool(re.search(r'\d+', line_time))
    return (has_car_info and has_time_info)

def chinese_to_num(text):
    cn_num = {'零':0, '一':1, '二':2, '三':3, '四':4, '五':5, '六':6, '七':7, '八':8, '九':9, '十':10}
    def repl_ten(m):
        p1 = m.group(1)
        p2 = m.group(2)
        v1 = cn_num.get(p1, 1) if p1 else 1
        v2 = cn_num.get(p2, 0) if p2 else 0
        return str(v1 * 10 + v2)
    t = re.sub(r'([一二三四五六七八九])?十([一二三四五六七八九])?', repl_ten, text)
    for k, v in cn_num.items():
        if k != '十':
            t = t.replace(k, str(v))
    return t

def extract_order_code(text):
    # 精準匹配 #S0/ 或 #S0 或 #A101，抓出斜線前的代碼
    match = re.search(r'#([a-zA-Z0-9]+)', text)
    if match:
        return match.group(1).upper()
    return None

def extract_core_address(text):
    t = text
    # 1. 移除括號內容
    t = re.sub(r'\[.*?\]', '', t)
    # 2. 徹底清掉 #S0/ 或 #S0 這種開頭前綴（包含斜線）
    t = re.sub(r'#[a-zA-Z0-9]+/?', '', t)
    # 3. 清除時間
    t = re.sub(r'\d{1,2}[:：點\.]\d{2}?', '', t)
    # 4. 清除雜詞
    t = re.sub(r'(轉帳|改兩台|客下街口|\+\d+)', '', t)
    # 5. 中文數字轉阿拉伯數字
    t = chinese_to_num(t)
    # 6. 只保留中英文與數字
    t = re.sub(r'[^\w\u4e00-\u9fa5]', '', t)
    return t.strip()

def is_new_order_format(text):
    if not text:
        return False
    first_line = text.split('\n')[0].strip()
    # 只要開頭是 #、* 或包含 ┼ 均視為新單據格式
    if first_line.startswith("#") or first_line.startswith("*") or "┼" in first_line:
        return True
    return False

def intercept_event(event):
    user_id = getattr(event.source, 'user_id', None)
    if user_id and user_id in BLOCKED_USER_IDS:
        return True
    if isinstance(event, MessageEvent) and isinstance(event.message, TextMessage):
        text = event.message.text.strip()
        if len(text) > 1000:
            return True
        for kw in BLOCKED_KEYWORDS:
            if kw in text:
                return True
    return False

# ----------------------------------------------------
# 📌 路由
# ----------------------------------------------------
@bot_bp.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    
    if not signature:
        return 'OK', 200

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return 'OK', 200
    except Exception as e:
        print(f"❌ Callback 處理異常: {e}")
        traceback.print_exc()
        return 'OK', 200
        
    return 'OK', 200

@bot_bp.route('/api/get_unassigned', methods=['GET'])
def api_get_unassigned():
    unassigned_orders = []
    now = datetime.now()
    cutoff_time = now - timedelta(minutes=10)
    with sheet_lock:
        for row in sheet_data[1:]:
            if len(row) >= 2 and row[1] == "未派出":
                order_time_str = row[4] if len(row) >= 5 else ""
                if order_time_str:
                    try:
                        order_dt = datetime.strptime(order_time_str, "%Y-%m-%d %H:%M")
                        if cutoff_time <= order_dt <= now:
                            unassigned_orders.append(row[0].strip())
                    except Exception:
                        unassigned_orders.append(row[0].strip())
                else:
                    unassigned_orders.append(row[0].strip())
    return jsonify({"orders": unassigned_orders})

@bot_bp.route('/liff', methods=['GET'])
def liff_page():
    return render_template('liff.html')

# ----------------------------------------------------
# 🟢 事件監聽
# ----------------------------------------------------
@handler.add(UnsendEvent)
def handle_unsend(event):
    if intercept_event(event):
        return
    unsend_msg_id = event.unsend.message_id
    with sheet_lock:
        target_row_idx = msg_id_to_row.get(unsend_msg_id)
        if target_row_idx and target_row_idx <= len(sheet_data):
            sheet_data[target_row_idx - 1][1] = "取消"
            write_queue.put(("update_cell", (target_row_idx, 2, "取消")))
            print(f"🚫 [收回取消成功] 第 {target_row_idx} 列已排入取消更新")

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    if intercept_event(event):
        print("🛡️ [訊息被安全過濾器攔截]")
        return

    user_msg = event.message.text.strip()
    user_id = event.source.user_id
    msg_id = event.message.id

    print(f"📩 [收到 LINE 訊息]: {repr(user_msg)}")

    if user_msg.startswith("試算") or user_msg.startswith("車資"):
        match = re.search(r'(?:試算|車資)\s*([\d\.]+)\s*([\d\.]+)', user_msg)
        if match:
            km = float(match.group(1))
            mins = float(match.group(2))
            fare_result = calculate_fare(km, mins)
            line_bot_api.reply_message(event.reply_token, TextMessage(text=fare_result))
            return
        else:
            line_bot_api.reply_message(event.reply_token, TextMessage(text="⚠️ 試算格式不正確！請輸入：`試算 25 30`"))
            return

    if user_msg in ["整單", "一鍵整單"]:
        flex_msg = FlexSendMessage(
            alt_text="一鍵整單助理",
            contents=BubbleContainer(
                size='nano',
                body=BoxComponent(
                    layout='vertical',
                    padding_all='10px',
                    spacing='sm',
                    contents=[
                        TextComponent(text="158派單助理", weight="bold", size="xs", color="#aaaaaa", align="center"),
                        ButtonComponent(style='primary', color='#1DB446', height='sm', action=URIAction(label='一鍵整單', uri=f'https://liff.line.me/{LIFF_ID}'))
                    ]
                )
            )
        )
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    sender_name = "司機"
    try:
        if event.source.type == 'group':
            group_id = event.source.group_id
            profile = line_bot_api.get_group_member_profile(group_id, user_id)
            sender_name = profile.display_name
        else:
            profile = line_bot_api.get_profile(user_id)
            sender_name = profile.display_name
    except Exception:
        sender_name = f"司機({user_id[-4:]})"

    has_mention = "@" in user_msg
    has_dispatch_kw = any(kw in user_msg for kw in ["噴", "出", "待出發", "走", "好", "可", "ok", "OK"])

    with sheet_lock:
        # A. 群組 tag 核銷
        if has_mention and has_dispatch_kw:
            mentioned_names = re.findall(r'@([^\s]+)', user_msg)
            for idx, row in enumerate(sheet_data[1:], start=2):
                if len(row) >= 3 and row[1] in ["未派出", "預約等待中"]:
                    c_cell_text = row[2]
                    matched = False
                    for m_name in mentioned_names:
                        clean_m_name = m_name.strip()
                        if clean_m_name and (f"[{clean_m_name}]" in c_cell_text or clean_m_name in c_cell_text):
                            matched = True
                            break
                    if matched:
                        sheet_data[idx - 1][1] = "已派出"
                        sheet_data[idx - 1][3] = f"核銷調度:{sender_name}"
                        write_queue.put(("update_cell", (idx, 2, "已派出")))
                        write_queue.put(("update_cell", (idx, 4, f"核銷調度:{sender_name}")))
                        print(f"🚀 [調度核銷排入佇列] 第 {idx} 列 -> 已派出")
                        return

        first_line = user_msg.split('\n')[0].strip()
        incoming_code = extract_order_code(first_line)
        incoming_core = extract_core_address(user_msg)

        print(f"🔍 [解析單據] 單號代碼: {incoming_code} | 核心地址: {incoming_core}")

        same_order_same_addr_idx = None
        for idx, row in enumerate(sheet_data[1:], start=2):
            if not len(row) >= 1 or not row[0].strip():
                continue
            existing_raw = row[0].strip()
            existing_code = extract_order_code(existing_raw)
            existing_core = extract_core_address(existing_raw)

            if user_msg == existing_raw or first_line == existing_raw:
                same_order_same_addr_idx = idx
                break
            if incoming_code and existing_code and incoming_code == existing_code:
                if not incoming_core and not existing_core:
                    same_order_same_addr_idx = idx
                    break
                elif incoming_core and existing_core and (incoming_core in existing_core or existing_core in incoming_core):
                    same_order_same_addr_idx = idx
                    break

        # B. 司機回報
        if same_order_same_addr_idx is not None:
            existing_c_cell = sheet_data[same_order_same_addr_idx - 1][2].strip()
            if is_valid_driver_report(user_msg):
                formatted_report = f"[{sender_name}]\n{user_msg}"
                if existing_c_cell:
                    new_c_content = f"{existing_c_cell}\n\n--------------------\n{formatted_report}"
                else:
                    new_c_content = formatted_report
                sheet_data[same_order_same_addr_idx - 1][2] = new_c_content
                write_queue.put(("update_cell", (same_order_same_addr_idx, 3, new_c_content)))
                print(f"📝 [司機回報排入佇列] 第 {same_order_same_addr_idx} 列更新")
                return
            else:
                print(f"ℹ️ [比對到相同單號/地址，但非司機回報格式，跳過]: 列 {same_order_same_addr_idx}")
                return

        # C. 新增單據
        else:
            if is_new_order_format(user_msg):
                booking_time = parse_booking_time(user_msg)
                new_status = "預約等待中" if booking_time else "未派出"
                new_type = "預約單" if booking_time else "即時"

                new_row = [
                    user_msg, 
                    new_status, 
                    "", 
                    "", 
                    booking_time if booking_time else "", 
                    new_type
                ]
                
                sheet_data.append(new_row)
                target_row = len(sheet_data)
                msg_id_to_row[msg_id] = target_row
                write_queue.put(("append", new_row))
                
                print(f"⚡ [成功排入佇列] 新單號: {first_line} | 狀態: {new_status}")
                return
            else:
                print(f"⚠️ [未符合新單據格式]: {first_line}")

# ----------------------------------------------------
# 🚀 Flask 主實體
# ----------------------------------------------------
app = Flask(__name__)
app.register_blueprint(bot_bp)

@app.route("/", methods=["GET"])
def index():
    return "Bot Listener is running!", 200

# 啟動時自動載入快取與非同步背景佇列
init_bot_listener()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)