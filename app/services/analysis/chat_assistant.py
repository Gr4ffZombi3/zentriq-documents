"""KI-Chat-Assistent, strikt auf die Daten des angemeldeten Vermittlers beschraenkt (jede
Tool-Funktion filtert ueber assigned_user_id, nie tenant-weit). Acht feste Funktionen statt
eines generischen SQL-Generators, per OpenAI Function Calling ausgewaehlt - exakt das Muster
aus app/services/llm/search_parser.py: die KI waehlt nur aus einer festen Signatur, es gibt
keinen Text-to-SQL-Pfad. Der Antworttext wird deterministisch aus den Abfrageergebnissen
komponiert (kein zweiter GPT-Aufruf), minimiert Kosten und Halluzinationsrisiko. V1 bewusst
zustandslos (kein persistierter Gespraechsverlauf) - siehe Abschlussbericht."""

import json
from datetime import date, timedelta

from flask import current_app

from app.models import Customer, DocumentCustomer, Recommendation, Task
from app.models.enums import Priority, RecommendationStatus, RecommendationType, TaskStatus, TaskType
from app.services.analysis.business_rules import count_offer_occurrences, customer_has_ever_closed
from app.services.llm.client import get_openai_client
from app.services.potential_score import compute_potential_score
from app.services.wiedervorlagen import get_open_offer_customer_dates

_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2}


def _customer_products(customer_id: int) -> set[str]:
    doc_customers = DocumentCustomer.query.filter_by(customer_id=customer_id).all()
    products: set[str] = set()
    for doc_customer in doc_customers:
        for row in doc_customer.row_data or []:
            products.update(p.lower() for p in (row.get("products") or []))
    return products


def tool_list_customers_to_call_today(user_id: int) -> list[dict]:
    today = date.today()
    tasks = Task.query.filter(
        Task.assigned_user_id == user_id,
        Task.type == TaskType.CALL_TODAY,
        Task.status == TaskStatus.OPEN,
        Task.due_date <= today,
    ).all()
    return [
        {"customer_id": t.customer_id, "customer_name": t.customer.name if t.customer else None, "task": t.title}
        for t in tasks
    ]


def tool_list_customers_with_single_offer_only(user_id: int) -> list[dict]:
    customers = Customer.query.filter_by(assigned_user_id=user_id).all()
    return [
        {"customer_id": c.id, "customer_name": c.name}
        for c in customers
        if count_offer_occurrences(c.id) == 1 and not customer_has_ever_closed(c.id)
    ]


def tool_list_customers_with_no_response(user_id: int) -> list[dict]:
    customer_ids = {c.id for c in Customer.query.filter_by(assigned_user_id=user_id).all()}
    today = date.today()
    result = []
    for customer_id, offer_date in get_open_offer_customer_dates().items():
        if customer_id not in customer_ids:
            continue
        days_since = (today - offer_date).days
        if days_since >= 7:
            customer = Customer.query.filter_by(id=customer_id).first()
            result.append(
                {"customer_id": customer_id, "customer_name": customer.name if customer else None, "days_since_offer": days_since}
            )
    return result


def tool_list_customers_with_only_product(user_id: int, product: str) -> list[dict]:
    product_lower = product.lower()
    customers = Customer.query.filter_by(assigned_user_id=user_id).all()
    return [{"customer_id": c.id, "customer_name": c.name} for c in customers if _customer_products(c.id) == {product_lower}]


def tool_list_customers_missing_product(user_id: int, product: str) -> list[dict]:
    product_lower = product.lower()
    customers = Customer.query.filter_by(assigned_user_id=user_id).all()
    result = []
    for customer in customers:
        products = _customer_products(customer.id)
        if products and product_lower not in products:
            result.append({"customer_id": customer.id, "customer_name": customer.name})
    return result


def tool_list_customers_by_sales_risk(user_id: int) -> list[dict]:
    customer_ids = {c.id for c in Customer.query.filter_by(assigned_user_id=user_id).all()}
    recommendations = Recommendation.query.filter(
        Recommendation.type == RecommendationType.SALES_RISK_MULTIPLE_OFFERS_NO_CLOSURE,
        Recommendation.status == RecommendationStatus.OPEN,
    ).all()
    return [
        {"customer_id": r.customer_id, "customer_name": r.customer.name if r.customer else None, "reason": r.explanation or r.label}
        for r in recommendations
        if r.customer_id in customer_ids
    ]


def tool_list_customers_by_cross_sell_potential(user_id: int) -> list[dict]:
    customers = Customer.query.filter_by(assigned_user_id=user_id).all()
    scored = []
    for customer in customers:
        doc_customers = DocumentCustomer.query.filter_by(customer_id=customer.id).all()
        products: set[str] = set()
        cross_sell = False
        multi_products = False
        priorities: list[str] = []
        for doc_customer in doc_customers:
            for row in doc_customer.row_data or []:
                products.update(row.get("products") or [])
                cross_sell = cross_sell or bool(row.get("cross_sell_opportunity"))
                multi_products = multi_products or bool(row.get("has_multiple_products"))
                priorities.append(row.get("priority", "medium"))
        if not products and not cross_sell:
            continue
        priority = Priority(max(priorities, key=lambda p: _PRIORITY_RANK.get(p, 1))) if priorities else Priority.MEDIUM
        score = compute_potential_score(
            priority=priority, products=sorted(products), cross_sell_opportunity=cross_sell, has_multiple_products=multi_products
        )
        if score > 0:
            scored.append({"customer_id": customer.id, "customer_name": customer.name, "score": score})
    scored.sort(key=lambda entry: entry["score"], reverse=True)
    return scored[:10]


def tool_list_stale_tasks(user_id: int) -> list[dict]:
    cutoff = date.today() - timedelta(days=30)
    tasks = Task.query.filter(
        Task.assigned_user_id == user_id, Task.status == TaskStatus.OPEN, Task.due_date < cutoff
    ).all()
    return [
        {
            "customer_id": t.customer_id,
            "customer_name": t.customer.name if t.customer else None,
            "task": t.title,
            "due_date": t.due_date.isoformat() if t.due_date else None,
        }
        for t in tasks
    ]


TOOLS = {
    "list_customers_to_call_today": tool_list_customers_to_call_today,
    "list_customers_with_single_offer_only": tool_list_customers_with_single_offer_only,
    "list_customers_with_no_response": tool_list_customers_with_no_response,
    "list_customers_with_only_product": tool_list_customers_with_only_product,
    "list_customers_missing_product": tool_list_customers_missing_product,
    "list_customers_by_sales_risk": tool_list_customers_by_sales_risk,
    "list_customers_by_cross_sell_potential": tool_list_customers_by_cross_sell_potential,
    "list_stale_tasks": tool_list_stale_tasks,
}

_NO_ARGS_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
_PRODUCT_ARG_SCHEMA = {
    "type": "object",
    "properties": {"product": {"type": "string", "description": "Produkt-/Sparten-Name, z.B. KFZ oder Privathaftpflicht"}},
    "required": ["product"],
    "additionalProperties": False,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_customers_to_call_today",
            "description": "Kunden, die heute angerufen werden sollten.",
            "parameters": _NO_ARGS_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_customers_with_single_offer_only",
            "description": "Kunden, die bisher nur ein einziges Angebot erhalten haben und noch keinen Abschluss.",
            "parameters": _NO_ARGS_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_customers_with_no_response",
            "description": "Kunden mit offenem Angebot ohne Rückmeldung seit mindestens 7 Tagen.",
            "parameters": _NO_ARGS_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_customers_with_only_product",
            "description": "Kunden, die ausschließlich das angegebene Produkt besitzen.",
            "parameters": _PRODUCT_ARG_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_customers_missing_product",
            "description": "Kunden, denen das angegebene Produkt fehlt.",
            "parameters": _PRODUCT_ARG_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_customers_by_sales_risk",
            "description": "Kunden mit dem höchsten Abschlussrisiko (mehrere Angebote ohne Abschluss).",
            "parameters": _NO_ARGS_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_customers_by_cross_sell_potential",
            "description": "Kunden mit dem höchsten Cross-Selling-Potenzial, absteigend sortiert.",
            "parameters": _NO_ARGS_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_stale_tasks",
            "description": "Offene Vorgänge, die älter als 30 Tage sind.",
            "parameters": _NO_ARGS_SCHEMA,
        },
    },
]

CHAT_SYSTEM_PROMPT = (
    "Du hilfst einem Versicherungsvermittler, Informationen über seine eigenen Kunden und "
    "Aufgaben zu finden. Wähle GENAU EINE der verfügbaren Funktionen passend zur Frage. "
    "Erfinde keine Werte für Parameter - übernimm nur, was eindeutig aus der Frage hervorgeht."
)

_FALLBACK_ANSWER = "Ich konnte deine Frage nicht eindeutig einer bekannten Abfrage zuordnen."
_ERROR_ANSWER = "Bei der Beantwortung ist ein Fehler aufgetreten. Bitte versuche es erneut."


def _compose_answer(results: list[dict]) -> str:
    if not results:
        return "Dazu habe ich aktuell keine passenden Einträge gefunden."
    names = [r["customer_name"] for r in results if r.get("customer_name")]
    if names:
        shown = ", ".join(names[:10])
        suffix = "…" if len(names) > 10 else ""
        return f"{len(results)} Treffer: {shown}{suffix}"
    return f"{len(results)} Treffer gefunden."


def answer_chat_query(user_id: int, question: str) -> dict:
    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model=current_app.config["OPENAI_MODEL"],
            messages=[
                {"role": "system", "content": CHAT_SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
        )
        message = response.choices[0].message
        if not message.tool_calls:
            return {"answer": _FALLBACK_ANSWER, "tool_used": None, "results": []}

        tool_call = message.tool_calls[0]
        tool_fn = TOOLS.get(tool_call.function.name)
        if tool_fn is None:
            return {"answer": _FALLBACK_ANSWER, "tool_used": None, "results": []}

        arguments = json.loads(tool_call.function.arguments or "{}")
        results = tool_fn(user_id, **arguments)
        return {"answer": _compose_answer(results), "tool_used": tool_call.function.name, "results": results}
    except Exception:
        return {"answer": _ERROR_ANSWER, "tool_used": None, "results": []}
