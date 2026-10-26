import gspread
from google.oauth2.service_account import Credentials
import requests

# ==================== 設定區 ====================
SHEET_NAME = "調度室派單管理表"

# 如果要透過 LINE Notify 或 Bot 廣播整單訊息，可在這裡設定 Token
# 這裡提供直接讀取 Google Sheet 並格式化輸出的完整範例
# ================================================

def get_unassigned_orders():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file('credentials.json', scopes=scopes)
    gs_client = gspread.authorize(creds)
    sheet = gs_client.open(SHEET_NAME).sheet1

    records = sheet.get_all_values()
    
    unassigned_list = []
    
    # 跳過第 1 列標頭
    for row in records[1:]:
        if len(row) >= 2 and row[1] == "未派出":
            order_content = row[0].strip()
            # 如果單據已有司機回報 (C 欄有值)，可以視需求加上標註，或只抓原始單據
            unassigned_list.append(order_content)

    return unassigned_list

def generate_dispatch_message():
    orders = get_unassigned_orders()
    
    if not orders:
        print("目前沒有未派出的單據！")
        return ""

    message = "📋 【目前未派出單據清單】\n"
    message += "------------------------\n"
    for idx, order in enumerate(orders, start=1):
        message += f"{order}\n"
    message += "------------------------\n"
    message += f"總計：{len(orders)} 筆未派單"
    
    return message

if __name__ == "__main__":
    formatted_msg = generate_dispatch_message()
    print(formatted_msg)