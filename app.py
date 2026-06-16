import ipaddress
import json
import re
import threading
import traceback
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, url_for, flash, Response
)

from werkzeug.middleware.proxy_fix import ProxyFix

from tricount_manager import TricountManager
from recurring_manager import RecurringManager
from connection_manager import ConnectionManager
from config import SECRET_KEY, DEBUG, CREDENTIALS_PATH, APP_PASSWORD, PERMANENT_SESSION_LIFETIME_DAYS

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.debug = DEBUG
app.permanent_session_lifetime = timedelta(days=PERMANENT_SESSION_LIFETIME_DAYS)

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

manager = TricountManager()
rec_manager = RecurringManager()
conn_manager = ConnectionManager()

_lock = threading.Lock()

LAN_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
]

LAN_EXCLUDE = {
    ipaddress.ip_address("10.0.10.110"),
    ipaddress.ip_address("192.168.2.149"),
}


def _is_lan():
    addr = request.remote_addr
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr)
        if ip in LAN_EXCLUDE:
            return False
        return any(ip in net for net in LAN_NETWORKS)
    except ValueError:
        return False


def _is_auth():
    return not APP_PASSWORD or _is_lan() or session.get("_auth")


def require_app_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if _is_auth():
            return f(*args, **kwargs)
        if request.is_json:
            return jsonify({"error": "Unauthorized"}), 401
        flash("Veuillez vous connecter", "warning")
        return redirect(url_for("index"))
    return decorated


def require_tricount(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "tricount_token" not in session or not manager.tricount:
            if request.is_json:
                return jsonify({"error": "Not connected to any tricount"}), 401
            flash("Connectez-vous d'abord à un tricount", "warning")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


def extract_token(raw):
    raw = raw.strip().rstrip("/")
    if not raw:
        return ""

    # Si c'est une URL tricount.com, prendre le dernier segment du chemin
    if "tricount.com" in raw:
        path = raw.split("tricount.com", 1)[1].lstrip("/")
        parts = path.split("/")
        last = parts[-1] if parts else ""
        if re.match(r"^[A-Za-z0-9]{8,40}$", last):
            return last
        return ""

    # Token brut avec préfixe t: tXXXXX (exclure "tricount" lui-même)
    if re.match(r"^t[A-Za-z0-9]{6,40}$", raw) and raw.lower() != "tricount":
        return raw

    # Token brut sans préfixe (nouveau format long)
    if re.match(r"^[A-Za-z0-9]{10,40}$", raw):
        return raw

    return ""


def process_recurring(skip_refresh=False):
    token = session.get("tricount_token")
    if not token:
        return
    with _lock:
        due = rec_manager.get_due(token)
        if not due:
            return
        if not skip_refresh:
            try:
                manager.refresh_tricount()
            except Exception:
                return
        for rec in due:
            try:
                tx_id = _execute_recurring(rec)
                next_run = rec_manager.compute_next_run(
                    rec["frequency"], rec["interval_count"],
                    from_date=rec["next_run_date"],
                    day_of_week=rec.get("day_of_week"),
                    day_of_month=rec.get("day_of_month"),
                )
                rec_manager.mark_executed(rec["id"], next_run, transaction_id=str(tx_id))
            except Exception as e:
                next_run = rec_manager.compute_next_run(
                    rec["frequency"], rec["interval_count"],
                    from_date=rec["next_run_date"],
                    day_of_week=rec.get("day_of_week"),
                    day_of_month=rec.get("day_of_month"),
                )
                rec_manager.mark_executed(rec["id"], next_run, error=str(e))


def _execute_recurring(rec):
    members = {m.uuid: m for m in manager.members}
    payer = members.get(rec["payer_uuid"])
    if not payer:
        raise ValueError(f"Payer UUID {rec['payer_uuid']} not found in tricount members")

    split_members = rec.get("split_members")
    split_uuids = set(split_members) if split_members else set()

    if rec["split_mode"] == "equal":
        if split_uuids:
            split_among = [members[u] for u in split_uuids if u in members]
        else:
            split_among = list(members.values())
        if not split_among:
            raise ValueError("No participants selected for recurring expense")
        return manager.create_transaction(
            description=rec["description"],
            amount=rec["amount"],
            payer=payer,
            split_among=split_among,
            category=rec["category"],
            date=date.today().isoformat(),
        )
    else:
        split_among = list(members.values())
        return manager.create_transaction(
            description=rec["description"],
            amount=rec["amount"],
            payer=payer,
            split_among=split_among,
            category=rec["category"],
            date=date.today().isoformat(),
        )


# ---------- Auth ----------

@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    password = request.form.get("password", "")
    if not APP_PASSWORD:
        return jsonify({"error": "Aucun mot de passe configuré"}), 500
    if password != APP_PASSWORD:
        return jsonify({"error": "Mot de passe incorrect"}), 403
    session.permanent = request.form.get("remember_me") == "1"
    session["_auth"] = True
    return jsonify({"success": True})


@app.route("/api/auth/check")
def api_auth_check():
    return jsonify({"authenticated": _is_auth()})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.clear()
    return jsonify({"success": True})


# ---------- Routes ----------

@app.route("/api/debug-token")
@require_app_auth
def api_debug_token():
    """Debug a specific token: shows what the API returns."""
    raw = request.args.get("token", "").strip()
    if not raw:
        return jsonify({"error": "token parameter required"}), 400
    parsed = extract_token(raw)
    result = {"raw_input": raw, "parsed_token": parsed}
    if not parsed:
        result["error"] = "Could not extract token"
        return jsonify(result)
    try:
        import requests as req
        from tricount import Credentials, TricountAPI
        creds = Credentials.load(Path(CREDENTIALS_PATH))
        client = TricountAPI(creds)
        client.authenticate()
        r = client.session.get(
            f"https://api.tricount.bunq.com/v1/user/{client.user_id}/registry",
            params={"public_identifier_token": parsed},
        )
        result["lookup_status"] = r.status_code
        result["lookup_body"] = r.text[:2000]
    except Exception as e:
        result["debug_error"] = str(e)
    return jsonify(result)


@app.route("/api/diagnostic")
@require_app_auth
def api_diagnostic():
    """Test API connectivity and return debug info. Destroys stale credentials if needed."""
    import sys
    info = {
        "python_version": sys.version,
        "credentials_file_exists": str(Path(CREDENTIALS_PATH).exists()),
        "credentials_path": CREDENTIALS_PATH,
        "can_reach_api": False,
        "auth_works": False,
        "error": None,
    }
    try:
        import requests
        r = requests.get("https://api.tricount.bunq.com/v1/", timeout=10)
        info["api_response"] = r.status_code
        info["can_reach_api"] = True
    except Exception as e:
        info["error"] = f"API unreachable: {e}"
        return jsonify(info)

    try:
        from tricount import load_client
        creds_path = Path(CREDENTIALS_PATH)
        try:
            client = load_client(str(creds_path))
            info["auth_works"] = True
        except Exception as e:
            info["auth_error"] = str(e)
            if creds_path.exists():
                creds_path.unlink()
                info["credentials_reset"] = True
                try:
                    client = load_client(str(creds_path))
                    info["auth_works"] = True
                except Exception as e2:
                    info["error"] = f"Auth failed even after reset: {e2}"
    except Exception as e:
        info["error"] = f"Module error: {e}"

    return jsonify(info)


@app.route("/")
def index():
    return render_template("index.html",
                         connected="tricount_token" in session)


@app.route("/api/connect", methods=["POST"])
@require_app_auth
def api_connect():
    raw_token = request.form.get("token", "").strip()
    if not raw_token:
        return jsonify({"error": "Token requis"}), 400
    token = extract_token(raw_token)
    if not token:
        return jsonify({"error": "Impossible de reconnaître le token. Copiez le lien de partage ou le token (ex: tABC123xyz)."}), 400
    try:
        print(f"[CONNECT] Using token: {token!r}")
        tricount = manager.join_tricount(token)
        session["tricount_token"] = tricount.public_identifier_token
        session["tricount_title"] = tricount.title
        process_recurring(skip_refresh=True)
        balances = manager.get_balances()
        return jsonify({
            "success": True,
            "title": tricount.title,
            "currency": tricount.currency,
            "members": [{"uuid": m.uuid, "name": m.display_name} for m in tricount.members],
            "transactions": _serialize_transactions(tricount.transactions),
            "balances": {name: round(bal, 2) for name, bal in balances.items()},
        })
    except Exception as e:
        tb = traceback.format_exc()
        print(f"CONNECT ERROR: {tb}")
        return jsonify({"error": f"Token utilisé: '{token}'. Erreur de connexion: {str(e)}"}), 400


@app.route("/api/disconnect", methods=["POST"])
@require_app_auth
def api_disconnect():
    session.pop("tricount_token", None)
    session.pop("tricount_title", None)
    manager.tricount = None
    return jsonify({"success": True})


@app.route("/api/data")
@require_app_auth
@require_tricount
def api_data():
    try:
        manager.refresh_tricount()
        process_recurring(skip_refresh=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    balances = manager.get_balances()
    return jsonify({
        "title": manager.tricount.title,
        "currency": manager.tricount.currency,
        "members": [{"uuid": m.uuid, "name": m.display_name} for m in manager.members],
        "transactions": _serialize_transactions(manager.transactions),
        "balances": {name: round(bal, 2) for name, bal in balances.items()},
    })


@app.route("/api/transaction/create", methods=["POST"])
@require_app_auth
@require_tricount
def api_create_transaction():
    description = request.form.get("description", "").strip()
    amount = request.form.get("amount", "0")
    payer_uuid = request.form.get("payer_uuid", "")
    split_mode = request.form.get("split_mode", "equal")
    category = request.form.get("category", "")
    tx_date = request.form.get("date", date.today().isoformat())

    if not description or not payer_uuid:
        return jsonify({"error": "Description et payeur requis"}), 400

    try:
        amount = float(amount)
    except ValueError:
        return jsonify({"error": "Montant invalide"}), 400

    members = {m.uuid: m for m in manager.members}
    payer = members.get(payer_uuid)
    if not payer:
        return jsonify({"error": "Payeur invalide"}), 400

    try:
        split_members_raw = request.form.get("split_members", "")
        split_uuids = set()
        if split_members_raw:
            try:
                split_uuids = set(json.loads(split_members_raw))
            except json.JSONDecodeError:
                pass

        if split_mode == "equal":
            if split_uuids:
                split_among = [members[u] for u in split_uuids if u in members]
            else:
                split_among = list(members.values())
            if not split_among:
                return jsonify({"error": "Aucun participant sélectionné"}), 400
            tx_id = manager.create_transaction(
                description=description, amount=amount, payer=payer,
                split_among=split_among,
                category=category, date=tx_date,
            )
        elif split_mode == "reimbursement":
            receiver_uuid = request.form.get("receiver_uuid", "")
            receiver = members.get(receiver_uuid)
            if not receiver:
                return jsonify({"error": "Bénéficiaire invalide"}), 400
            tx_id = manager.create_reimbursement(
                payer=payer, receiver=receiver, amount=amount,
                description=description, date=tx_date,
            )
        else:
            return jsonify({"error": "Mode de répartition invalide"}), 400

        manager.refresh_tricount()
        balances = manager.get_balances()
        return jsonify({
            "success": True,
            "transaction_id": tx_id,
            "transactions": _serialize_transactions(manager.transactions),
            "balances": {name: round(bal, 2) for name, bal in balances.items()},
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/transaction/edit", methods=["POST"])
@require_app_auth
@require_tricount
def api_edit_transaction():
    tx_id = request.form.get("transaction_id", "")
    description = request.form.get("description", "").strip()
    amount = request.form.get("amount", "0")
    category = request.form.get("category", "")
    tx_date = request.form.get("date", "")

    if not tx_id:
        return jsonify({"error": "ID de transaction requis"}), 400
    try:
        amount = float(amount)
    except ValueError:
        return jsonify({"error": "Montant invalide"}), 400

    try:
        manager.edit_transaction(
            transaction_id=int(tx_id),
            description=description,
            amount=amount,
            category=category,
            date=tx_date or None,
        )
        manager.refresh_tricount()
        balances = manager.get_balances()
        return jsonify({
            "success": True,
            "transactions": _serialize_transactions(manager.transactions),
            "balances": {name: round(bal, 2) for name, bal in balances.items()},
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/transaction/delete", methods=["POST"])
@require_app_auth
@require_tricount
def api_delete_transaction():
    tx_id = request.form.get("transaction_id", "")
    if not tx_id:
        return jsonify({"error": "ID de transaction requis"}), 400
    try:
        manager.delete_transaction(transaction_id=int(tx_id))
        manager.refresh_tricount()
        balances = manager.get_balances()
        return jsonify({
            "success": True,
            "transactions": _serialize_transactions(manager.transactions),
            "balances": {name: round(bal, 2) for name, bal in balances.items()},
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/members/add", methods=["POST"])
@require_app_auth
@require_tricount
def api_add_members():
    names_raw = request.form.get("names", "")
    names = [n.strip() for n in names_raw.split(",") if n.strip()]
    if not names:
        return jsonify({"error": "Noms requis (séparés par des virgules)"}), 400
    try:
        manager.add_members(names)
        manager.refresh_tricount()
        return jsonify({
            "success": True,
            "members": [{"uuid": m.uuid, "name": m.display_name} for m in manager.members],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/members/rename", methods=["POST"])
@require_app_auth
@require_tricount
def api_rename_member():
    member_uuid = request.form.get("member_uuid", "")
    new_name = request.form.get("new_name", "").strip()
    if not member_uuid or not new_name:
        return jsonify({"error": "Membre et nouveau nom requis"}), 400
    members = {m.uuid: m for m in manager.members}
    member = members.get(member_uuid)
    if not member:
        return jsonify({"error": "Membre non trouvé"}), 400
    try:
        manager.rename_member(member, new_name)
        manager.refresh_tricount()
        return jsonify({
            "success": True,
            "members": [{"uuid": m.uuid, "name": m.display_name} for m in manager.members],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/members/delete", methods=["POST"])
@require_app_auth
@require_tricount
def api_delete_member():
    member_uuid = request.form.get("member_uuid", "")
    if not member_uuid:
        return jsonify({"error": "UUID du membre requis"}), 400
    members = {m.uuid: m for m in manager.members}
    member = members.get(member_uuid)
    if not member:
        return jsonify({"error": "Membre non trouvé"}), 400
    try:
        manager.delete_member(member)
        manager.refresh_tricount()
        return jsonify({
            "success": True,
            "members": [{"uuid": m.uuid, "name": m.display_name} for m in manager.members],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ---------- Recurring routes ----------

@app.route("/api/recurring/list")
@require_app_auth
@require_tricount
def api_recurring_list():
    token = session["tricount_token"]
    recs = rec_manager.list_recurring(token)
    return jsonify({"recurring": recs})


@app.route("/api/recurring/create", methods=["POST"])
@require_app_auth
@require_tricount
def api_recurring_create():
    token = session["tricount_token"]
    description = request.form.get("description", "").strip()
    amount = request.form.get("amount", "0")
    payer_uuid = request.form.get("payer_uuid", "")
    frequency = request.form.get("frequency", "monthly")
    interval_count = request.form.get("interval_count", "1")
    split_mode = request.form.get("split_mode", "equal")
    category = request.form.get("category", "")
    start_date = request.form.get("start_date", date.today().isoformat())

    if not description or not payer_uuid:
        return jsonify({"error": "Description et payeur requis"}), 400
    try:
        amount = float(amount)
    except ValueError:
        return jsonify({"error": "Montant invalide"}), 400
    try:
        interval_count = int(interval_count)
    except ValueError:
        return jsonify({"error": "Intervalle invalide"}), 400
    if frequency not in ("daily", "weekly", "biweekly", "monthly"):
        return jsonify({"error": "Fréquence invalide"}), 400

    day_of_week = request.form.get("day_of_week")
    if day_of_week is not None:
        try:
            day_of_week = int(day_of_week)
        except (ValueError, TypeError):
            day_of_week = None
    day_of_month = request.form.get("day_of_month")
    if day_of_month is not None:
        try:
            day_of_month = int(day_of_month)
        except (ValueError, TypeError):
            day_of_month = None

    split_data = None
    if split_mode == "custom":
        allocations_raw = request.form.get("allocations", "[]")
        try:
            split_data = json.loads(allocations_raw)
        except json.JSONDecodeError:
            return jsonify({"error": "Allocations invalides"}), 400

    split_members_raw = request.form.get("split_members", "")
    split_members = None
    if split_members_raw:
        try:
            split_members = json.loads(split_members_raw)
        except json.JSONDecodeError:
            pass

    try:
        rec_id = rec_manager.add_recurring(
            tricount_token=token,
            description=description,
            amount=amount,
            payer_uuid=payer_uuid,
            frequency=frequency,
            interval_count=interval_count,
            split_mode=split_mode,
            split_data=split_data,
            split_members=split_members,
            category=category,
            start_date=start_date,
            day_of_week=day_of_week,
            day_of_month=day_of_month,
        )
        return jsonify({"success": True, "recurring_id": rec_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/recurring/edit", methods=["POST"])
@require_app_auth
@require_tricount
def api_recurring_edit():
    rec_id = request.form.get("recurring_id", "")
    if not rec_id:
        return jsonify({"error": "ID requis"}), 400
    try:
        rec_id = int(rec_id)
    except ValueError:
        return jsonify({"error": "ID invalide"}), 400

    kwargs = {}
    for field in ("description", "amount", "payer_uuid", "frequency",
                  "interval_count", "split_mode", "category", "is_active",
                  "day_of_week", "day_of_month"):
        val = request.form.get(field)
        if val is not None:
            if field == "interval_count":
                try:
                    val = int(val)
                except ValueError:
                    continue
            elif field == "amount":
                try:
                    val = float(val)
                except ValueError:
                    continue
            elif field == "is_active":
                val = 1 if val in ("1", "true", "on") else 0
            elif field in ("day_of_week", "day_of_month"):
                try:
                    val = int(val)
                except (ValueError, TypeError):
                    continue
            kwargs[field] = val

    if request.form.get("split_data"):
        try:
            kwargs["split_data"] = json.loads(request.form["split_data"])
        except json.JSONDecodeError:
            return jsonify({"error": "Allocations invalides"}), 400

    if request.form.get("split_members"):
        try:
            kwargs["split_members"] = json.loads(request.form["split_members"])
        except json.JSONDecodeError:
            pass

    start_date = request.form.get("start_date")
    if start_date:
        kwargs["next_run_date"] = start_date

    try:
        rec_manager.update_recurring(rec_id, **kwargs)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/recurring/delete", methods=["POST"])
@require_app_auth
@require_tricount
def api_recurring_delete():
    rec_id = request.form.get("recurring_id", "")
    if not rec_id:
        return jsonify({"error": "ID requis"}), 400
    try:
        rec_manager.delete_recurring(int(rec_id))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/recurring/logs")
@require_app_auth
@require_tricount
def api_recurring_logs():
    rec_id = request.args.get("recurring_id")
    logs = rec_manager.get_logs(rec_id=int(rec_id) if rec_id else None)
    return jsonify({"logs": logs})


# ---------- Connection Management ----------

@app.route("/api/connections/list")
@require_app_auth
def api_connections_list():
    return jsonify({"connections": conn_manager.list_all()})


@app.route("/api/connections/add", methods=["POST"])
@require_app_auth
def api_connections_add():
    name = request.form.get("name", "").strip()
    token_raw = request.form.get("token", "").strip()
    if not name or not token_raw:
        return jsonify({"error": "Nom et token requis"}), 400
    token = extract_token(token_raw)
    if not token:
        return jsonify({"error": "Token invalide"}), 400
    try:
        rec_id = conn_manager.add(name, token)
        return jsonify({"success": True, "id": rec_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/connections/edit", methods=["POST"])
@require_app_auth
def api_connections_edit():
    rec_id = request.form.get("id", "")
    name = request.form.get("name", "").strip()
    token_raw = request.form.get("token", "").strip()
    if not rec_id:
        return jsonify({"error": "ID requis"}), 400
    kwargs = {}
    if name:
        kwargs["name"] = name
    if token_raw:
        token = extract_token(token_raw)
        if token:
            kwargs["token"] = token
    if not kwargs:
        return jsonify({"error": "Rien à modifier"}), 400
    try:
        conn_manager.update(int(rec_id), **kwargs)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/connections/delete", methods=["POST"])
@require_app_auth
def api_connections_delete():
    rec_id = request.form.get("id", "")
    if not rec_id:
        return jsonify({"error": "ID requis"}), 400
    try:
        conn_manager.delete(int(rec_id))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ---------- Helpers ----------

CATEGORY_LABELS = {
    "TRAVEL": "Voyage",
    "ENTERTAINMENT": "Loisirs",
    "GROCERIES": "Courses",
    "HEALTHCARE": "Santé",
    "INSURANCE": "Assurance",
    "RENT_AND_UTILITIES": "Loyer & Charges",
    "FOOD_AND_DRINK": "Restaurant",
    "SHOPPING": "Shopping",
    "TRANSPORT": "Transport",
    "OTHER": "Autre",
}


def _serialize_transactions(transactions):
    result = []
    members_map = {m.uuid: m.display_name for m in manager.members}
    for tx in transactions:
        allocations = []
        for alloc in tx.allocations:
            allocations.append({
                "uuid": alloc.membership_uuid,
                "name": members_map.get(alloc.membership_uuid, "Inconnu"),
                "amount": round(alloc.amount.as_float, 2),
            })
        raw = tx.date or ""
        display_date = raw.split(" ")[0] if " " in raw else raw
        cat = tx.category
        if cat in (None, "", "Uncategorize", "Uncategorized"):
            cat = None
        result.append({
            "id": tx.id,
            "uuid": tx.uuid,
            "description": tx.description,
            "amount": round(tx.amount.as_float, 2),
            "abs_amount": round(tx.amount.as_abs, 2),
            "payer_name": members_map.get(tx.membership_uuid_owner, "Inconnu"),
            "payer_uuid": tx.membership_uuid_owner,
            "allocations": allocations,
            "date": display_date,
            "type": tx.transaction_type,
            "category": cat,
            "category_label": CATEGORY_LABELS.get(cat) if cat else None,
            "category_custom": tx.category_custom,
            "status": tx.status,
        })
    result.sort(key=lambda t: t["date"] or "", reverse=True)
    return result


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=DEBUG)
