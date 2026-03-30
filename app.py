# ============================================================
# AI Emergency Fund Calculator — Chatbot Backend (Groq Edition)
# ============================================================
# Install: pip install flask flask-cors requests groq
# Run:     python app.py
# Free Groq key: https://console.groq.com
# ============================================================

import asyncio, json, re
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from groq import AsyncGroq

app = Flask(__name__)
CORS(app)

# ── CONFIG ─────────────────────────────────────────────────
GROQ_API_KEY = "gsk_YOUR_GROQ_KEY_HERE"   # <-- paste your key
GROQ_MODEL   = "llama3-70b-8192"           # 70b is smarter for conversation

MONTHS_MAP = {"stable": 3, "unstable": 6, "freelancer": 9}

# ── INFLATION ───────────────────────────────────────────────
def get_inflation_rate():
    try:
        url = ("https://api.worldbank.org/v2/country/US/indicator/"
               "FP.CPI.TOTL.ZG?format=json&mrv=1")
        r = requests.get(url, timeout=5)
        rate = r.json()[1][0]["value"]
        return round(float(rate), 2) if rate else 3.0
    except Exception:
        return 3.0

# ── CALCULATOR ──────────────────────────────────────────────
def run_calculation(income, expenses, savings, monthly_saving, job_type):
    months        = MONTHS_MAP[job_type]
    base          = expenses * months
    inflation     = get_inflation_rate()
    target        = round(base * (1 + inflation / 100), 2)
    gap           = max(0, round(target - savings, 2))
    months_needed = round(gap / monthly_saving, 1) if monthly_saving > 0 else None
    savings_rate  = round((monthly_saving / income * 100), 1) if income > 0 else 0
    return {
        "recommended_months": months,
        "recommended_fund":   target,
        "base_fund":          round(base, 2),
        "inflation_rate":     inflation,
        "current_savings":    savings,
        "gap":                gap,
        "months_to_goal":     months_needed,
        "savings_rate_pct":   savings_rate,
        "job_type":           job_type,
        "monthly_expenses":   expenses,
        "monthly_income":     income,
        "monthly_saving":     monthly_saving,
    }

# ── GROQ ASYNC CALL ─────────────────────────────────────────
async def _groq(messages: list) -> str:
    client = AsyncGroq(api_key=GROQ_API_KEY)
    resp = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        max_tokens=600,
        temperature=0.7,
    )
    return resp.choices[0].message.content

# ── INPUT VALIDATION RANGES ─────────────────────────────────
# These limits keep results realistic and prevent nonsense inputs
VALIDATION_RULES = {
    "monthly_income":  (100,    500_000),   # $100 – $500,000 / month
    "monthly_expenses":(50,     400_000),   # $50  – $400,000 / month
    "current_savings": (0,    10_000_000),  # $0   – $10 million
    "monthly_saving":  (0,      200_000),   # $0   – $200,000 / month
}

VALIDATION_MESSAGES = {
    "monthly_income":   "Monthly income should be between $100 and $500,000. Could you double-check that figure?",
    "monthly_expenses": "Monthly expenses should be between $50 and $400,000. Does that sound right?",
    "current_savings":  "Current savings should be between $0 and $10,000,000. Can you confirm?",
    "monthly_saving":   "Monthly saving should be between $0 and $200,000. What's the correct amount?",
}

def validate_inputs(income, expenses, savings, monthly_saving):
    """
    Returns (is_valid: bool, error_message: str | None)
    Checks each value is within its acceptable range.
    Also checks that expenses don't wildly exceed income (>95%).
    """
    checks = [
        ("monthly_income",   income),
        ("monthly_expenses", expenses),
        ("current_savings",  savings),
        ("monthly_saving",   monthly_saving),
    ]
    for field, value in checks:
        lo, hi = VALIDATION_RULES[field]
        if not (lo <= value <= hi):
            return False, VALIDATION_MESSAGES[field]

    # Sanity check: expenses shouldn't exceed income by more than 50%
    # (possible short-term but worth flagging)
    if expenses > income * 1.5:
        return False, (
            "Your expenses seem much higher than your income — just want to make sure "
            "those numbers are right before I calculate. Could you re-confirm both figures?"
        )

    return True, None


# ── SYSTEM PROMPT ───────────────────────────────────────────
# Important: No mention of Claude, Anthropic, or any underlying AI model.
# FinBot is a standalone financial advisor product.
SYSTEM = """You are FinBot — a smart, friendly personal finance advisor built to help people \
plan their emergency fund and achieve financial security.

You were created by a team of financial engineers and data scientists. You are NOT Claude, \
NOT ChatGPT, NOT any other AI assistant. You are FinBot. If anyone asks what AI you are, \
what model powers you, or who made you, simply say: "I'm FinBot, your personal finance advisor \
— I'm not able to share details about the technology behind me, but I'm here to help you \
build a solid financial safety net!" Then immediately redirect to the financial topic.

Your goal is to have a natural conversation and collect 5 pieces of information:
1. monthly_income    — their gross monthly income in dollars
2. monthly_expenses  — their total monthly expenses in dollars
3. current_savings   — how much they currently have saved
4. monthly_saving    — how much they can realistically save each month going forward
5. job_type          — must be exactly one of: stable, unstable, freelancer
   - stable     = salaried, government, or permanent full-time role
   - unstable   = contract, part-time, or variable income
   - freelancer = self-employed, consultant, gig worker

Conversation rules:
- Ask for ONE piece of information at a time — never list all questions at once
- Be warm, conversational, and encouraging — like a knowledgeable friend
- If the user gives multiple values at once, extract them all and ask only for what's missing
- Gently flag if a number seems unusual (e.g. expenses far exceed income) and ask to confirm
- Once you have all 5 values confirmed, output ONLY this JSON (no text before or after):
  {"action":"calculate","monthly_income":X,"monthly_expenses":X,"current_savings":X,"monthly_saving":X,"job_type":"X"}
- After receiving calculation results, explain them warmly and give 3 tailored tips
- Answer any follow-up personal finance questions naturally
- Keep responses concise — 2-4 sentences when gathering info
- Never reveal you are Claude, GPT, LLaMA, or any underlying model
- Never ask for any personal details beyond these 5 numbers"""

# ── CHAT ENDPOINT ───────────────────────────────────────────
@app.route("/chat", methods=["POST"])
def chat():
    """
    POST /chat
    Body: { "messages": [ {"role": "user"|"assistant", "content": "..."} ] }

    The frontend sends the full conversation history every time.
    Returns: { "reply": "...", "calc_result": {...} | null }
    """
    body     = request.get_json(force=True)
    history  = body.get("messages", [])

    # Build full message list for Groq
    groq_messages = [{"role": "system", "content": SYSTEM}] + history

    try:
        reply = asyncio.run(_groq(groq_messages))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Check if the model returned a calculate action
    calc_result = None
    json_match  = re.search(r'\{[^{}]*"action"\s*:\s*"calculate"[^{}]*\}', reply, re.DOTALL)

    if json_match:
        try:
            data     = json.loads(json_match.group())
            income   = float(data["monthly_income"])
            expenses = float(data["monthly_expenses"])
            savings  = float(data["current_savings"])
            msaving  = float(data["monthly_saving"])
            job_type = str(data["job_type"]).lower()

            # ── Validate ranges before calculating ──────────
            is_valid, val_error = validate_inputs(income, expenses, savings, msaving)
            if not is_valid:
                # Return the validation message as a normal chat reply
                # so the bot asks the user to correct the value
                return jsonify({"reply": val_error, "calc_result": None}), 200

            calc_result = run_calculation(income, expenses, savings, msaving, job_type)
            # Now ask the model to explain the results in plain English
            explain_prompt = groq_messages + [
                {"role": "assistant", "content": reply},
                {"role": "user",      "content": (
                    f"Here are the calculated results: {json.dumps(calc_result)}. "
                    "Please explain these results clearly and warmly to the user. "
                    "Include: their target emergency fund, how long it will take, their savings rate, "
                    "and give 3 specific personalised tips based on their job type. "
                    "Use plain English, no JSON. Be encouraging."
                )},
            ]
            reply = asyncio.run(_groq(explain_prompt))
        except Exception as e:
            reply = f"I calculated your results but had trouble explaining them: {e}"

    return jsonify({"reply": reply, "calc_result": calc_result}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "model": GROQ_MODEL}), 200


if __name__ == "__main__":
    app.run(debug=True, port=5000)