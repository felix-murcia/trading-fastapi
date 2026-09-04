with open("fastapi/routers/ai.py", "r") as f:
    text = f.read()

# Replace the act_val == 0 logic
target = """            elif act_val == 0:
                ml_prob = 0.5
                decision = "CLOSE" if req.position != 0 else "HOLD\""""

replacement = """            elif act_val == 0:
                ml_prob = 0.5
                # Filter 'Scared AI' churning: Model is undertrained and panics on act_val 0. 
                # We enforce Stop & Reverse (SAR) by ignoring 0 and relying on opposite signals (1 or 2) or TP/SL to close.
                decision = "HOLD\""""

text = text.replace(target, replacement)

with open("fastapi/routers/ai.py", "w") as f:
    f.write(text)
