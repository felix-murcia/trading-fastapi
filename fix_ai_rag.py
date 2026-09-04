with open("fastapi/routers/ai.py", "r") as f:
    content = f.read()

# Replace block from "# 3. LLM Query" to "# 5. Telemetry & Auditing"
start_str = "# 3. LLM Query"
end_str = "# 5. Telemetry & Auditing"

start_idx = content.find(start_str)
end_idx = content.find(end_str)

new_block = """# 3. LLM Query & Macro RAG
    llm_bias = "NEUTRAL"
    if decision in ["BUY", "SELL"]: # Only ask LLM if we are opening a trade
        try:
            from services.news_scraper import get_macro_news
            live_news = await get_macro_news(req.symbol)
            prompt = f"Live News:\\n{live_news}\\n\\nAnalyze current {req.symbol} macro context. Bias:"
            
            async with httpx.AsyncClient() as client:
                res = await client.post(QWEN_URL, json={
                    "messages": [
                        {"role": "system", "content": "You are a quantitative macro analyst. Reply exactly one word: BULLISH, BEARISH, or NEUTRAL."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.1, "max_tokens": 5
                }, timeout=3.0)
                
                if res.status_code == 200:
                    respuesta = res.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip().upper()
                    if "BULL" in respuesta: llm_bias = "BULLISH"
                    elif "BEAR" in respuesta: llm_bias = "BEARISH"
        except Exception:
            pass
            
    # 4. Hybrid Decision Rules
    # If PPO wants to BUY/SELL, we let Qwen veto it on conflicting macro.
    if decision == "BUY" and llm_bias == "BEARISH":
        decision = "HOLD" # Vetoed
    elif decision == "SELL" and llm_bias == "BULLISH":
        decision = "HOLD" # Vetoed
    # (If decision == "CLOSE" or "HOLD", it passes freely without LLM interference!)
    
    """

full_new_content = content[:start_idx] + new_block + content[end_idx:]

with open("fastapi/routers/ai.py", "w") as f:
    f.write(full_new_content)
