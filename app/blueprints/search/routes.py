from flask import Blueprint, render_template, request

from app.search.query_builder import fallback_text_search, search_documents
from app.services.llm.search_parser import parse_search_query

search_bp = Blueprint("search", __name__, url_prefix="/search")


@search_bp.route("")
def search():
    query = request.args.get("q", "").strip()
    documents = []
    used_fallback = False

    if query:
        filter_spec = parse_search_query(query)
        if filter_spec is not None and filter_spec.model_dump(exclude_none=True):
            documents = search_documents(filter_spec)
        else:
            documents = fallback_text_search(query)
            used_fallback = True

    return render_template(
        "search/results.html", query=query, documents=documents, used_fallback=used_fallback
    )
