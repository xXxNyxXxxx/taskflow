from flask import Blueprint

board_bp = Blueprint("board", __name__, url_prefix="/tasks")

from app.blueprints.board import routes  # noqa: E402, F401