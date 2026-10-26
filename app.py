import os
from flask import Flask, render_template, jsonify
from bot_listener import bot_bp, get_sheet

app = Flask(__name__)

# 註冊來自 bot_listener.py 的 Webhook 路由
app.register_blueprint(bot_bp)

# ---------------------------------------------------------
# LIFF 網頁 API & 頁面路由
# ---------------------------------------------------------
@app.route('/api/get_unassigned', methods=['GET'])
def api_get_unassigned():
    try:
        sheet = get_sheet()
        records = sheet.get_all_values()

        unassigned_orders = []
        for row in records[1:]:
            if len(row) >= 2 and row[1] == "未派出":
                unassigned_orders.append(row[0])

        return jsonify({"orders": unassigned_orders})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/liff')
def liff_page():
    return render_template('liff.html')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)