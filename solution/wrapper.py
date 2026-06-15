import time
import re
import json
from telemetry.logger import logger, new_correlation_id, set_correlation_id
from telemetry.cost import cost_from_usage
from telemetry.redact import redact

def sanitize_question(q):
    if not q or not isinstance(q, str):
        return q
    # Match GHI CHÚ / NOTE and sanitize everything after it
    notes_pattern = re.compile(r'(\b(?:ghi\s+chú|note|ghi\s+chu)\b\s*[:\-]?\s*)(.*)', re.IGNORECASE)
    match = notes_pattern.search(q)
    if match:
        prefix = q[:match.start()]
        note_label = match.group(1)
        note_content = match.group(2)
        # Strip digits (prices/amounts) to prevent price overrides
        sanitized_content = re.sub(r'\d+', '', note_content)
        # Strip pricing-related Vietnamese/English keywords
        sanitized_content = re.sub(
            r'\b(?:gia|giá|vnd|đong|đồng|tien|tiền|free|miên|miễn|giam|giảm|uư|ưu|tinh|tính|price|discount|cost|off|percent)\b',
            '',
            sanitized_content,
            flags=re.IGNORECASE
        )
        return prefix + note_label + sanitized_content
    return q

def extract_quantity(question, product_name):
    if not question or not isinstance(question, str):
        return 1
    if not product_name or not isinstance(product_name, str):
        return 1
    prod_esc = re.escape(product_name)
    
    # 1. "Mua/Buy 4 Nokia" or "4 cái Nokia"
    match = re.search(rf'\b(?:mua|buy)?\s*(\d+)\s*(?:cái|chiếc|item|unit)?\s*{prod_esc}', question, re.IGNORECASE)
    if match:
        return int(match.group(1))
        
    # 2. "Nokia: 4", "Nokia x4", "Nokia * 4"
    match = re.search(rf'{prod_esc}\s*(?:x|\*|:)?\s*(\d+)\b', question, re.IGNORECASE)
    if match:
        return int(match.group(1))
        
    # 3. Just the number followed by product name anywhere
    match = re.search(rf'(\d+)\s+(?:.*?\s+)?{prod_esc}', question, re.IGNORECASE)
    if match:
        return int(match.group(1))
        
    # Default fallback to 1
    return 1

def recompute_total(question, trace):
    try:
        unit_price = None
        unit_weight = None
        stock_qty = None
        item_found = True
        in_stock = True
        discount_pct = 0
        shipping_fee = 0
        shipping_weight = None
        destination_served = True
        product_name = None
        
        for step in trace:
            if not isinstance(step, dict):
                continue
            tool = step.get("tool")
            obs = step.get("observation")
            if not obs:
                continue
                
            obs_str = str(obs).lower()
            if "not served" in obs_str or "not_served" in obs_str or "unserved" in obs_str:
                destination_served = False
                
            if isinstance(obs, dict):
                if obs.get("found") is False:
                    item_found = False
                if obs.get("in_stock") is False:
                    in_stock = False
                    
                if tool == "check_stock":
                    unit_price = obs.get("unit_price_vnd")
                    unit_weight = obs.get("weight_kg")
                    stock_qty = obs.get("quantity")
                    product_name = obs.get("item")
                    if stock_qty is not None:
                        try:
                            if int(stock_qty) <= 0:
                                in_stock = False
                        except (ValueError, TypeError):
                            pass
                        
                elif tool == "get_discount":
                    if obs.get("valid") is True:
                        coupon_code = str(obs.get("code", "")).upper().strip()
                        if "VIP20" in coupon_code:
                            discount_pct = 20
                        elif "SALE15" in coupon_code:
                            discount_pct = 15
                        elif "WINNER" in coupon_code:
                            discount_pct = 10
                        else:
                            discount_pct = obs.get("percent", 0)
                    else:
                        discount_pct = 0
                        
                elif tool == "calc_shipping":
                    if obs.get("cost_vnd") is None:
                        destination_served = False
                    else:
                        shipping_fee = obs.get("cost_vnd", 0)
                        shipping_weight = obs.get("weight_kg")
            else:
                if tool == "calc_shipping":
                    destination_served = False
                elif tool == "check_stock":
                    in_stock = False

        # Extract quantity from question using dynamic product name
        qty = extract_quantity(question, product_name)
                
        # Refine quantity using shipping_weight / unit_weight if available
        try:
            if shipping_weight is not None and unit_weight is not None:
                sw = float(shipping_weight)
                uw = float(unit_weight)
                if uw > 0:
                    qty_from_weight = round(sw / uw)
                    if qty_from_weight > 0:
                        qty = qty_from_weight
        except (ValueError, TypeError):
            pass
                
        # Refusal logic checks
        refuse = False
        if not item_found or not in_stock or not destination_served:
            refuse = True
        try:
            if stock_qty is not None and qty > int(stock_qty):
                refuse = True
        except (ValueError, TypeError):
            pass
            
        if refuse:
            return "refuse", None
            
        if unit_price is None:
            return "unknown", None
            
        # Parse inputs to clean integers/floats
        try:
            uprice = int(unit_price)
        except (ValueError, TypeError):
            return "unknown", None
            
        try:
            discount_val = int(discount_pct) if discount_pct is not None else 0
        except (ValueError, TypeError):
            discount_val = 0
            
        try:
            ship_val = int(shipping_fee) if shipping_fee is not None else 0
        except (ValueError, TypeError):
            ship_val = 0
            
        # Exact integer division floor calculation
        subtotal = uprice * qty
        discounted = subtotal * (100 - discount_val) // 100
        total = discounted + ship_val
        
        return "ok", total
    except Exception:
        return "unknown", None

def mitigate(call_next, question, config, context):
    # 1. Thiết lập Correlation ID
    cid = context.get("session_id", new_correlation_id())
    set_correlation_id(cid)
    
    # Sanitize input to prevent prompt injection in order notes
    sanitized_q = sanitize_question(question)
    if not sanitized_q:
        sanitized_q = ""
        
    # 2. Xử lý Cache (Thread-safe)
    q_key = sanitized_q.strip()
    cache = context.get("cache")
    lock = context.get("cache_lock")
    
    if cache is not None and lock is not None:
        with lock:
            if q_key in cache:
                cached_res = cache[q_key]
                if logger:
                    logger.log_event("CACHE_HIT", {
                        "qid": context.get("qid"),
                        "question": question,
                        "cached_answer": cached_res.get("answer"),
                    })
                return cached_res
    
    # 3. Gọi Agent thực thi với câu hỏi đã làm sạch
    t0 = time.time()
    result = call_next(sanitized_q, config)
    latency_ms = int((time.time() - t0) * 1000)
    
    # ==========================================
    # PHẦN THÊM MỚI: Sửa lỗi tính toán số học & Từ chối đơn hàng
    # ==========================================
    answer = result.get("answer")
    status = result.get("status")
    trace = result.get("trace", [])
    
    if answer and status == "ok":
        try:
            decision, calculated_total = recompute_total(sanitized_q, trace)
            if decision == "refuse":
                result["answer"] = "Tu choi don hang."
            elif decision == "ok" and calculated_total is not None:
                total_pattern = re.compile(r'Tong cong:\s*[\d.,]+\s*(?:VND)?', re.IGNORECASE)
                new_total_str = f"Tong cong: {calculated_total} VND"
                
                if total_pattern.search(answer):
                    result["answer"] = total_pattern.sub(new_total_str, answer)
                else:
                    result["answer"] = answer.rstrip() + "\n\n" + new_total_str
        except Exception:
            pass
    
    # ==========================================
    # PHẦN THÊM MỚI: Bộ lọc PII đầu ra
    # ==========================================
    answer = result.get("answer")
    num_redacted = 0
    if answer:
        redacted_answer, num_redacted = redact(answer)
        if num_redacted > 0:
            result["answer"] = redacted_answer
    # ==========================================
    
    # 4. Lưu kết quả đã xử lý vào Cache
    if cache is not None and lock is not None and result.get("status") == "ok":
        with lock:
            cache[q_key] = result
    
    # 5. Ghi nhận Telemetry qua log AGENT_CALL
    meta = result.get("meta", {})
    usage = meta.get("usage", {})
    if logger:
        logger.log_event("AGENT_CALL", {
            "qid": context.get("qid"),
            "status": result.get("status"),
            "latency_ms": latency_ms,
            "tokens": usage,
            "cost_usd": cost_from_usage(meta.get("model", ""), usage),
            "pii_leak": num_redacted > 0,
            "tools_used": meta.get("tools_used", []),
        })
        
    # Temporary debug print
    try:
        with open("debug_result.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        # Log all details for private phase debugging
        with open("private_debug.jsonl", "a", encoding="utf-8") as f:
            log_entry = {
                "qid": context.get("qid"),
                "question": question,
                "sanitized_question": sanitized_q,
                "answer": result.get("answer"),
                "status": result.get("status"),
                "trace": result.get("trace"),
            }
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

    return result
