from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app.services.analysis.chat_assistant import answer_chat_query

chat_bp = Blueprint("chat", __name__, url_prefix="/api")


@chat_bp.route("/chat", methods=["POST"])
@login_required
def chat():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"answer": "Bitte stelle eine Frage.", "tool_used": None, "results": []}), 400
    return jsonify(answer_chat_query(current_user.id, question))
