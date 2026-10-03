import os
import traceback
from fastapi import FastAPI, Request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from supabase import create_client

app = FastAPI()

# เชื่อมต่อ LINE และ Supabase
line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET", ""))
supabase = create_client(os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_KEY", ""))

@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        return {"error": "Invalid signature"}
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    text = event.message.text.strip()
    
    try:
        parts = text.split()
        cmd = parts[0]

        # 1. เช็กสต็อก (ดึงชื่อจาก products และคำนวณต้นทุนจาก inventory_lots)
        if cmd == "?":
            sku = parts[1].upper()
            
            p_res = supabase.table("products").select("name").eq("sku", sku).execute()
            
            if not p_res.data:
                reply_msg = f"❌ ไม่พบข้อมูลสินค้ารหัส: {sku} ในระบบ"
            else:
                product_name = p_res.data[0]["name"]
                
                res = supabase.table("inventory_lots").select("*").eq("sku", sku).gt("qty_remain", 0).execute()
                lots = res.data
                total_qty = sum(lot["qty_remain"] for lot in lots)
                
                if total_qty == 0:
                    reply_msg = f"📦 {sku} : {product_name}\nสถานะ: สินค้าหมด (0 ชิ้น)"
                else:
                    total_value = sum(lot["qty_remain"] * lot["unit_cost"] for lot in lots)
                    avg_cost = total_value / total_qty
                    reply_msg = f"📦 {sku} : {product_name}\n🟢 คงเหลือ: {total_qty} ชิ้น\n📊 ทุนเฉลี่ย: {avg_cost:.2f} บาท/ชิ้น"

        # 2. รับของเข้า (แยก Lot และสร้างชื่อสินค้าให้อัตโนมัติถ้ายังไม่มี)
        elif cmd == "+":
            sku = parts[1].upper()
            qty = int(parts[2])
            cost = float(parts[3])
            
            # ถ้ามีการพิมพ์ชื่อสินค้าต่อท้ายมาด้วย จะเอาไปใช้ตั้งชื่อ ถ้าไม่มีจะใช้ SKU เป็นชื่อแทน
            product_name = " ".join(parts[4:]) if len(parts) > 4 else sku
            
            p_res = supabase.table("products").select("*").eq("sku", sku).execute()
            if not p_res.data:
                supabase.table("products").insert({"sku": sku, "name": product_name}).execute()
            
            supabase.table("inventory_lots").insert({
                "sku": sku, "qty_received": qty, "qty_remain": qty, "unit_cost": cost
            }).execute()
            
            reply_msg = f"✅ รับเข้าสำเร็จ\nสินค้า: {product_name} ({sku})\nเพิ่มสต็อก: {qty} ชิ้น\nต้นทุน: {cost} บาท/ชิ้น"

        # 3. ขายของออก (ตัดสต็อกแบบ FIFO)
        elif cmd == "-":
            sku = parts[1].upper()
            sell_qty = int(parts[2])
            
            res = supabase.table("inventory_lots").select("*").eq("sku", sku).gt("qty_remain", 0).order("created_at").execute()
            lots = res.data
            total_available = sum(lot["qty_remain"] for lot in lots)
            
            if total_available < sell_qty:
                reply_msg = f"❌ สต็อกไม่พอ!\n{sku} มีของพร้อมขายแค่ {total_available} ชิ้น"
            else:
                remain_to_deduct = sell_qty
                for lot in lots:
                    if remain_to_deduct <= 0: 
                        break
                    if lot["qty_remain"] <= remain_to_deduct:
                        remain_to_deduct -= lot["qty_remain"]
                        supabase.table("inventory_lots").update({"qty_remain": 0}).eq("lot_id", lot["lot_id"]).execute()
                    else:
                        new_qty = lot["qty_remain"] - remain_to_deduct
                        supabase.table("inventory_lots").update({"qty_remain": new_qty}).eq("lot_id", lot["lot_id"]).execute()
                        remain_to_deduct = 0
                reply_msg = f"📤 ตัดสต็อกสำเร็จ\nSKU: {sku}\nขายออก: {sell_qty} ชิ้น"
        
        # กรณีพิมพ์อย่างอื่นที่ไม่ได้ขึ้นต้นด้วย +, -, ?
        else:
            reply_msg = "คู่มือการใช้งาน:\n🔍 เช็กสต็อก: ? [SKU]\n📥 รับของเข้า: + [SKU] [จำนวน] [ทุน] [ชื่อสินค้า]\n📤 ตัดสต็อก: - [SKU] [จำนวน]"

    except Exception as e:
        reply_msg = f"❌ เกิดข้อผิดพลาดหลังบ้าน:\nError: {type(e).__name__}\n{str(e)}\n\n(ก๊อปปี้ข้อความนี้ส่งให้ผมดูได้เลยครับ)"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
