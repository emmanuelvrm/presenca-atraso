"""Presença & Atrasos — MVP (Flask + SQLite).

Fluxo:
  * Um QR code único aponta para /e.
  * No primeiro acesso o aluno informa nome, turma e matrícula; o servidor gera uma
    chave única do aparelho (cookie + localStorage) e vincula a matrícula a ele.
  * Nas aulas seguintes, escanear o QR já registra a chegada (login automático).
  * O professor abre a aula, acompanha só quem não leu o QR e quem está atrasado,
    confere as autorizações do pedagógico e gera o relatório.
"""
import csv
import io
import math
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import quote

import qrcode
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, send_file, session, url_for)

TZ = timezone(timedelta(hours=-3))  # America/Sao_Paulo (sem horário de verão desde 2019)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "presenca.db")


def _segredo(env, arquivo, nbytes, aviso=None):
    valor = os.environ.get(env)
    if valor:
        return valor
    caminho = os.path.join(DATA_DIR, arquivo)
    if os.path.exists(caminho):
        with open(caminho) as f:
            return f.read().strip()
    valor = secrets.token_urlsafe(nbytes)
    with open(caminho, "w") as f:
        f.write(valor)
    os.chmod(caminho, 0o600)
    if aviso:
        print(aviso.format(valor), flush=True)
    return valor


SECRET_KEY = _segredo("SECRET_KEY", ".secret", 32)
PROF_SENHA = _segredo("PROF_SENHA", ".senha", 9,
                      ">>> Senha do professor gerada: {}  (guardada em data/.senha)")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
PEDAGOGICO_EMAIL = os.environ.get("PEDAGOGICO_EMAIL", "")
TOLERANCIA_PADRAO = int(os.environ.get("TOLERANCIA_MIN", "10"))
CORTE_PADRAO = int(os.environ.get("CORTE_MIN", "90"))
UNI_LAT = float(os.environ["UNIDADE_LAT"]) if os.environ.get("UNIDADE_LAT") else None
UNI_LNG = float(os.environ["UNIDADE_LNG"]) if os.environ.get("UNIDADE_LNG") else None
RAIO_M = float(os.environ.get("RAIO_M", "250"))
GEOFENCE = UNI_LAT is not None and UNI_LNG is not None

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=BASE_URL.startswith("https"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=256 * 1024,
)

# Banco: SQLite por padrão; Postgres quando DATABASE_URL estiver definida (ex.: Render).
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = bool(DATABASE_URL)
if USE_PG:
    import psycopg
    from psycopg.rows import dict_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS turmas(
  id {PK}, nome TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS alunos(
  id {PK}, matricula TEXT UNIQUE NOT NULL, nome TEXT NOT NULL,
  turma_id INTEGER NOT NULL REFERENCES turmas(id),
  device_key TEXT UNIQUE, vinculado_em {BIG}, fora_lista INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS aulas(
  id {PK}, turma_id INTEGER NOT NULL REFERENCES turmas(id),
  disciplina TEXT, inicio {BIG} NOT NULL,
  tolerancia_min INTEGER NOT NULL DEFAULT 10, corte_min INTEGER NOT NULL DEFAULT 90,
  status TEXT NOT NULL DEFAULT 'aberta', criada_em {BIG}, encerrada_em {BIG});
CREATE TABLE IF NOT EXISTS leituras(
  id {PK}, aula_id INTEGER NOT NULL REFERENCES aulas(id),
  aluno_id INTEGER NOT NULL REFERENCES alunos(id),
  ts {BIG} NOT NULL, atraso_min INTEGER NOT NULL, manual INTEGER NOT NULL DEFAULT 0,
  autorizado INTEGER, lat {REAL}, lng {REAL}, dist_m {REAL}, fora INTEGER NOT NULL DEFAULT 0,
  UNIQUE(aula_id, aluno_id));
CREATE TABLE IF NOT EXISTS saidas(
  id {PK}, aula_id INTEGER NOT NULL REFERENCES aulas(id),
  aluno_id INTEGER NOT NULL REFERENCES alunos(id), ts {BIG} NOT NULL, motivo TEXT);
"""


def _schema_sql():
    tipos = {"{PK}": "SERIAL PRIMARY KEY", "{BIG}": "BIGINT", "{REAL}": "DOUBLE PRECISION"} if USE_PG else \
            {"{PK}": "INTEGER PRIMARY KEY", "{BIG}": "INTEGER", "{REAL}": "REAL"}
    sql = SCHEMA
    for k, v in tipos.items():
        sql = sql.replace(k, v)
    return [s.strip() for s in sql.split(";") if s.strip()]


def init_db():
    if USE_PG:
        con = psycopg.connect(DATABASE_URL, autocommit=True)
        for stmt in _schema_sql():
            try:
                con.execute(stmt)
            except psycopg.Error:  # outro worker criou ao mesmo tempo
                pass
        con.close()
        return
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    for stmt in _schema_sql():
        con.execute(stmt)
    con.commit()
    con.close()


init_db()


class _Cursor:
    def __init__(self, cur, lastrowid=None):
        self._c, self.lastrowid = cur, lastrowid

    @property
    def rowcount(self):
        return self._c.rowcount

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    def __iter__(self):
        return iter(self._c.fetchall())


class _PG:
    """Adapta a API usada no app (placeholders '?', lastrowid) para psycopg."""

    def __init__(self):
        self._con = psycopg.connect(DATABASE_URL, row_factory=dict_row)

    def execute(self, sql, params=()):
        sql = sql.replace("?", "%s")
        quer_id = sql.lstrip().upper().startswith("INSERT INTO") and "RETURNING" not in sql.upper()
        if quer_id:
            sql += " RETURNING id"
        cur = self._con.execute(sql, params)
        ultimo = None
        if quer_id:
            row = cur.fetchone()
            ultimo = row["id"] if row else None
        return _Cursor(cur, ultimo)

    def commit(self):
        self._con.commit()

    def close(self):
        self._con.close()


def db():
    if "db" not in g:
        if USE_PG:
            g.db = _PG()
        else:
            g.db = sqlite3.connect(DB_PATH, timeout=10)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def fechar_db(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


# ----------------------------------------------------------------- utilidades
def now():
    return int(time.time())


def hm(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M") if ts else ""


def dmy(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d/%m/%Y") if ts else ""


def parse_hm(texto):
    """'19:00' -> epoch de hoje (fuso de São Paulo) ou None."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", texto or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    agora = datetime.fromtimestamp(now(), TZ)
    alvo = agora.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    return int(alvo.timestamp())


def fmt_atraso(min_):
    if min_ < 60:
        return "%d min" % min_
    return "%dh%02d" % (min_ // 60, min_ % 60)


def norm_mat(s):
    return re.sub(r"\s+", "", s or "").upper()[:20]


def norm_turma(s):
    return re.sub(r"\s+", " ", (s or "").strip()).upper()[:40]


def norm_nome(s):
    return re.sub(r"\s+", " ", (s or "").strip())[:120]


def status_de(atraso_min, aula):
    if atraso_min <= aula["tolerancia_min"]:
        return "ok"
    if atraso_min <= aula["corte_min"]:
        return "atrasado"
    return "bloqueado"


def haversine(lat1, lng1, lat2, lng2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def avaliar_geo(geo):
    """Devolve (lat, lng, dist_m, fora). Só marca 'fora' se a unidade estiver configurada."""
    lat = lng = dist = None
    if isinstance(geo, dict):
        try:
            lat, lng = float(geo.get("lat")), float(geo.get("lng"))
        except (TypeError, ValueError):
            lat = lng = None
        if lat is not None and not (-90 <= lat <= 90 and -180 <= lng <= 180):
            lat = lng = None
    fora = 0
    if GEOFENCE:
        if lat is None:
            fora = 1
        else:
            dist = haversine(lat, lng, UNI_LAT, UNI_LNG)
            fora = 1 if dist > RAIO_M else 0
    return lat, lng, dist, fora


def url_entrada():
    return (BASE_URL or request.url_root.rstrip("/")) + "/e"


def aluno_do_aparelho():
    candidatos = [request.cookies.get("dk"), request.headers.get("X-Device-Key")]
    for chave in candidatos:
        if chave:
            row = db().execute(
                "SELECT a.*, t.nome AS turma FROM alunos a JOIN turmas t ON t.id=a.turma_id "
                "WHERE a.device_key=?", (chave,)).fetchone()
            if row:
                return row
    return None


def aula_aberta_da_turma(turma_id):
    limite = now() - 12 * 3600  # ignora aula esquecida aberta de outro dia
    return db().execute(
        "SELECT * FROM aulas WHERE turma_id=? AND status='aberta' AND inicio>? "
        "ORDER BY inicio DESC LIMIT 1", (turma_id, limite)).fetchone()


# ------------------------------------------------------------- aluno (público)
@app.get("/")
def index():
    return redirect(url_for("entrada"))


@app.get("/e")
def entrada():
    turmas = [t["nome"] for t in db().execute("SELECT nome FROM turmas ORDER BY nome")]
    resp = app.make_response(render_template("aluno.html", turmas=turmas, geofence=GEOFENCE))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/registrar")
def api_registrar():
    d = request.get_json(silent=True) or {}
    mat = norm_mat(d.get("matricula"))
    nome = norm_nome(d.get("nome"))
    turma_nome = norm_turma(d.get("turma"))
    if len(mat) < 4 or not nome or not turma_nome:
        return jsonify(ok=False, msg="Preencha nome, turma e matrícula."), 400
    con = db()
    atual = aluno_do_aparelho()
    aluno = con.execute("SELECT * FROM alunos WHERE matricula=?", (mat,)).fetchone()

    if atual and (aluno is None or atual["id"] != aluno["id"]):
        return jsonify(ok=False, msg="Este aparelho já está vinculado a outro aluno. "
                                     "Se for engano, peça ao professor para liberar."), 409
    if aluno is not None and aluno["device_key"] and (atual is None or atual["id"] != aluno["id"]):
        return jsonify(ok=False, msg="Esta matrícula já está vinculada a outro aparelho. "
                                     "Peça ao professor para liberar."), 409
    if aluno is None:
        turma = con.execute("SELECT id FROM turmas WHERE nome=?", (turma_nome,)).fetchone()
        if turma is None:
            return jsonify(ok=False, msg="Turma não encontrada."), 400
        cur = con.execute(
            "INSERT INTO alunos(matricula,nome,turma_id,fora_lista) VALUES (?,?,?,1)",
            (mat, nome, turma["id"]))
        aluno = con.execute("SELECT * FROM alunos WHERE id=?", (cur.lastrowid,)).fetchone()

    chave = aluno["device_key"] or secrets.token_urlsafe(24)
    con.execute("UPDATE alunos SET device_key=?, vinculado_em=COALESCE(vinculado_em,?) WHERE id=?",
                (chave, now(), aluno["id"]))
    con.commit()
    turma_nome_final = con.execute("SELECT nome FROM turmas WHERE id=?", (aluno["turma_id"],)).fetchone()["nome"]
    resp = jsonify(ok=True, key=chave, aluno={"nome": aluno["nome"], "matricula": aluno["matricula"],
                                              "turma": turma_nome_final})
    resp.set_cookie("dk", chave, max_age=60 * 60 * 24 * 730, httponly=True, samesite="Lax",
                    secure=request.is_secure)
    return resp


@app.post("/api/checkin")
def api_checkin():
    aluno = aluno_do_aparelho()
    if aluno is None:
        return jsonify(ok=False, precisa_cadastro=True, msg="Cadastro necessário."), 401
    d = request.get_json(silent=True) or {}
    con = db()
    perfil = {"nome": aluno["nome"], "matricula": aluno["matricula"], "turma": aluno["turma"]}
    aula = aula_aberta_da_turma(aluno["turma_id"])
    if aula is None:
        return jsonify(ok=False, perfil=perfil,
                       msg="Nenhuma aula aberta para a sua turma agora. "
                           "Se a aula já começou, avise o professor.")
    existente = con.execute("SELECT 1 FROM leituras WHERE aula_id=? AND aluno_id=?",
                            (aula["id"], aluno["id"])).fetchone()
    nova = existente is None
    if nova:
        t = now()
        atraso = max(0, (t - aula["inicio"]) // 60)
        lat, lng, dist, fora = avaliar_geo(d.get("geo"))
        con.execute(
            "INSERT INTO leituras(aula_id,aluno_id,ts,atraso_min,lat,lng,dist_m,fora) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING", (aula["id"], aluno["id"], t, atraso, lat, lng, dist, fora))
        con.commit()
    lt = con.execute("SELECT * FROM leituras WHERE aula_id=? AND aluno_id=?",
                     (aula["id"], aluno["id"])).fetchone()
    return jsonify(ok=True, nova=nova, perfil=perfil, disciplina=aula["disciplina"] or "",
                   inicio=hm(aula["inicio"]), chegada=hm(lt["ts"]), atraso_min=lt["atraso_min"],
                   atraso_txt=fmt_atraso(lt["atraso_min"]), status=status_de(lt["atraso_min"], aula),
                   corte_txt=fmt_atraso(aula["corte_min"]))


@app.get("/qr.png")
def qr_png():
    img = qrcode.make(url_entrada(), box_size=12, border=4,
                      error_correction=qrcode.constants.ERROR_CORRECT_M)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------- professor: auth
def seguro_next(destino):
    if destino and destino.startswith("/prof") and not destino.startswith("//"):
        return destino
    return url_for("prof_home")


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("prof"):
            if request.headers.get("X-Requested-With") == "fetch":
                return jsonify(ok=False, msg="Sessão expirada. Entre novamente."), 401
            return redirect(url_for("prof_login", next=request.path))
        return f(*a, **kw)
    return wrapper


@app.route("/prof/login", methods=["GET", "POST"])
def prof_login():
    erro = ""
    if request.method == "POST":
        senha = request.form.get("senha", "")
        if secrets.compare_digest(senha.encode(), PROF_SENHA.encode()):
            session.permanent = True
            session["prof"] = True
            return redirect(seguro_next(request.args.get("next")))
        time.sleep(1)
        erro = "Senha incorreta."
    return render_template("prof_login.html", erro=erro)


@app.get("/prof/sair")
def prof_sair():
    session.clear()
    return redirect(url_for("prof_login"))


# ---------------------------------------------------------- professor: painel
@app.get("/prof")
@login_required
def prof_home():
    con = db()
    turmas = con.execute(
        "SELECT t.id, t.nome, (SELECT COUNT(*) FROM alunos a WHERE a.turma_id=t.id) AS n "
        "FROM turmas t ORDER BY t.nome").fetchall()
    base = ("SELECT a.*, t.nome AS turma, "
            "(SELECT COUNT(*) FROM leituras l WHERE l.aula_id=a.id) AS leram "
            "FROM aulas a JOIN turmas t ON t.id=a.turma_id ")
    abertas = con.execute(base + "WHERE a.status='aberta' ORDER BY a.inicio DESC").fetchall()
    recentes = con.execute(base + "WHERE a.status<>'aberta' ORDER BY a.inicio DESC LIMIT 15").fetchall()
    ultima = con.execute("SELECT disciplina FROM aulas ORDER BY id DESC LIMIT 1").fetchone()
    return render_template("prof_home.html", turmas=turmas, abertas=abertas, recentes=recentes,
                           hm=hm, dmy=dmy, agora=hm(now()), tol=TOLERANCIA_PADRAO,
                           corte=CORTE_PADRAO, ultima_disciplina=(ultima["disciplina"] if ultima else ""))


@app.post("/prof/aulas")
@login_required
def prof_criar_aula():
    con = db()
    f = request.form
    try:
        turma_id = int(f.get("turma_id") or 0)
        tol = max(0, min(600, int(f.get("tolerancia") or TOLERANCIA_PADRAO)))
        corte = max(1, min(600, int(f.get("corte") or CORTE_PADRAO)))
    except ValueError:
        abort(400)
    turma = con.execute("SELECT id FROM turmas WHERE id=?", (turma_id,)).fetchone()
    if turma is None:
        flash("Escolha uma turma.", "erro")
        return redirect(url_for("prof_home"))
    inicio = parse_hm(f.get("inicio")) or now()
    t = now()
    con.execute("UPDATE aulas SET status='encerrada', encerrada_em=? WHERE turma_id=? AND status='aberta'",
                (t, turma_id))
    cur = con.execute(
        "INSERT INTO aulas(turma_id,disciplina,inicio,tolerancia_min,corte_min,criada_em) VALUES (?,?,?,?,?,?)",
        (turma_id, (f.get("disciplina") or "").strip()[:120], inicio, tol, corte, t))
    con.commit()
    return redirect(url_for("prof_aula", aula_id=cur.lastrowid))


def get_aula(aula_id):
    aula = db().execute("SELECT a.*, t.nome AS turma FROM aulas a JOIN turmas t ON t.id=a.turma_id "
                        "WHERE a.id=?", (aula_id,)).fetchone()
    if aula is None:
        abort(404)
    return aula


@app.get("/prof/aula/<int:aula_id>")
@login_required
def prof_aula(aula_id):
    return render_template("prof_aula.html", aula=get_aula(aula_id), hm=hm)


def calcular_estado(aula_id):
    con = db()
    aula = get_aula(aula_id)
    alunos = con.execute("SELECT * FROM alunos WHERE turma_id=? ORDER BY LOWER(nome)",
                         (aula["turma_id"],)).fetchall()
    leituras = {r["aluno_id"]: r for r in
                con.execute("SELECT * FROM leituras WHERE aula_id=?", (aula_id,))}
    saidas = con.execute(
        "SELECT s.id, s.ts, s.motivo, a.nome, a.matricula FROM saidas s JOIN alunos a ON a.id=s.aluno_id "
        "WHERE s.aula_id=? ORDER BY s.ts", (aula_id,)).fetchall()

    nao_leram, atrasados, atencao, nao_assinam = [], [], [], []
    no_horario = 0
    for al in alunos:
        base = {"id": al["id"], "matricula": al["matricula"], "nome": al["nome"]}
        lt = leituras.get(al["id"])
        if lt is None:
            nao_leram.append(base)
            nao_assinam.append(dict(base, motivo="Não leu o QR code"))
            continue
        st = status_de(lt["atraso_min"], aula)
        item = dict(base, chegada=hm(lt["ts"]), atraso_min=lt["atraso_min"],
                    atraso_txt=fmt_atraso(lt["atraso_min"]), status=st,
                    autorizado=lt["autorizado"], manual=bool(lt["manual"]))
        if st == "ok":
            no_horario += 1
        else:
            atrasados.append(item)
        if st == "bloqueado":
            nao_assinam.append(dict(base, motivo="Atraso de %s (acima de %s)" %
                                    (fmt_atraso(lt["atraso_min"]), fmt_atraso(aula["corte_min"]))))
        elif st == "atrasado" and lt["autorizado"] != 1:
            motivo = ("Atrasado, sem autorização" if lt["autorizado"] == 0
                      else "Atrasado, autorização ainda não conferida")
            nao_assinam.append(dict(base, motivo=motivo))
        flags = []
        if lt["fora"]:
            flags.append("leitura fora da unidade ou sem localização")
        if al["fora_lista"]:
            flags.append("matrícula fora da lista da turma")
        if flags:
            atencao.append(dict(item, flags=flags))

    agora = now()
    return {
        "aula": {"id": aula["id"], "turma": aula["turma"], "disciplina": aula["disciplina"] or "",
                 "data": dmy(aula["inicio"]), "inicio": hm(aula["inicio"]),
                 "tolerancia_min": aula["tolerancia_min"], "corte_min": aula["corte_min"],
                 "corte_txt": fmt_atraso(aula["corte_min"]),
                 "limite": hm(aula["inicio"] + aula["corte_min"] * 60),
                 "status": aula["status"], "agora": hm(agora),
                 "decorridos_min": max(0, (agora - aula["inicio"]) // 60)},
        "totais": {"turma": len(alunos), "no_horario": no_horario, "atrasados": len(atrasados),
                   "nao_leram": len(nao_leram), "nao_assinam": len(nao_assinam),
                   "a_conferir": sum(1 for a in atrasados if a["status"] == "atrasado" and a["autorizado"] is None)},
        "nao_leram": nao_leram, "atrasados": atrasados, "atencao": atencao,
        "nao_assinam": nao_assinam,
        "saidas": [{"id": s["id"], "nome": s["nome"], "matricula": s["matricula"],
                    "hora": hm(s["ts"]), "motivo": s["motivo"] or ""} for s in saidas],
        "alunos": [{"id": a["id"], "nome": a["nome"]} for a in alunos],
    }


@app.get("/prof/aula/<int:aula_id>/estado")
@login_required
def prof_estado(aula_id):
    resp = jsonify(calcular_estado(aula_id))
    resp.headers["Cache-Control"] = "no-store"
    return resp


def aluno_da_turma(aula, aluno_id):
    return db().execute("SELECT * FROM alunos WHERE id=? AND turma_id=?",
                        (aluno_id, aula["turma_id"])).fetchone()


@app.post("/prof/aula/<int:aula_id>/autorizacao")
@login_required
def prof_autorizacao(aula_id):
    d = request.get_json(silent=True) or {}
    valor = d.get("valor")
    if valor not in (1, 0, None):
        return jsonify(ok=False, msg="Valor inválido."), 400
    con = db()
    cur = con.execute("UPDATE leituras SET autorizado=? WHERE aula_id=? AND aluno_id=?",
                      (valor, aula_id, d.get("aluno_id")))
    con.commit()
    return jsonify(ok=cur.rowcount == 1)


@app.post("/prof/aula/<int:aula_id>/manual")
@login_required
def prof_manual(aula_id):
    aula = get_aula(aula_id)
    d = request.get_json(silent=True) or {}
    if aluno_da_turma(aula, d.get("aluno_id")) is None:
        return jsonify(ok=False, msg="Aluno não encontrado."), 404
    t = now()
    con = db()
    con.execute("INSERT INTO leituras(aula_id,aluno_id,ts,atraso_min,manual) VALUES (?,?,?,?,1) ON CONFLICT DO NOTHING",
                (aula_id, d["aluno_id"], t, max(0, (t - aula["inicio"]) // 60)))
    con.commit()
    return jsonify(ok=True)


@app.post("/prof/aula/<int:aula_id>/saida")
@login_required
def prof_saida(aula_id):
    aula = get_aula(aula_id)
    d = request.get_json(silent=True) or {}
    if aluno_da_turma(aula, d.get("aluno_id")) is None:
        return jsonify(ok=False, msg="Aluno não encontrado."), 404
    con = db()
    con.execute("INSERT INTO saidas(aula_id,aluno_id,ts,motivo) VALUES (?,?,?,?)",
                (aula_id, d["aluno_id"], now(), (d.get("motivo") or "").strip()[:200]))
    con.commit()
    return jsonify(ok=True)


@app.post("/prof/aula/<int:aula_id>/saida/<int:sid>/remover")
@login_required
def prof_saida_remover(aula_id, sid):
    con = db()
    con.execute("DELETE FROM saidas WHERE id=? AND aula_id=?", (sid, aula_id))
    con.commit()
    return jsonify(ok=True)


@app.post("/prof/aula/<int:aula_id>/encerrar")
@login_required
def prof_encerrar(aula_id):
    get_aula(aula_id)
    con = db()
    con.execute("UPDATE aulas SET status='encerrada', encerrada_em=? WHERE id=? AND status='aberta'",
                (now(), aula_id))
    con.commit()
    return redirect(url_for("prof_relatorio", aula_id=aula_id))


@app.post("/prof/aula/<int:aula_id>/reabrir")
@login_required
def prof_reabrir(aula_id):
    aula = get_aula(aula_id)
    con = db()
    con.execute("UPDATE aulas SET status='encerrada', encerrada_em=? WHERE turma_id=? AND status='aberta'",
                (now(), aula["turma_id"]))
    con.execute("UPDATE aulas SET status='aberta', encerrada_em=NULL WHERE id=?", (aula_id,))
    con.commit()
    return redirect(url_for("prof_aula", aula_id=aula_id))


# ----------------------------------------------------------- professor: turmas
LINHA_MAT_NOME = re.compile(r"^\s*([A-Za-z]*\d[\w-]*)\s*[;,\t ]\s*(.+?)\s*$")
LINHA_NOME_MAT = re.compile(r"^\s*(.+?)\s*[;,\t]\s*([A-Za-z]*\d[\w-]*)\s*$")


def parse_roster(texto):
    achados, ignoradas = [], 0
    for linha in (texto or "").splitlines():
        if not linha.strip():
            continue
        m = LINHA_MAT_NOME.match(linha)
        if m:
            achados.append((norm_mat(m.group(1)), norm_nome(m.group(2))))
            continue
        m = LINHA_NOME_MAT.match(linha)
        if m:
            achados.append((norm_mat(m.group(2)), norm_nome(m.group(1))))
        else:
            ignoradas += 1
    return achados, ignoradas


def importar_roster(turma_id, texto):
    con = db()
    achados, ignoradas = parse_roster(texto)
    novos = atualizados = 0
    for mat, nome in achados:
        ex = con.execute("SELECT id FROM alunos WHERE matricula=?", (mat,)).fetchone()
        if ex:
            con.execute("UPDATE alunos SET nome=?, turma_id=?, fora_lista=0 WHERE id=?",
                        (nome, turma_id, ex["id"]))
            atualizados += 1
        else:
            con.execute("INSERT INTO alunos(matricula,nome,turma_id) VALUES (?,?,?)", (mat, nome, turma_id))
            novos += 1
    con.commit()
    return novos, atualizados, ignoradas


@app.route("/prof/turmas", methods=["GET", "POST"])
@login_required
def prof_turmas():
    con = db()
    if request.method == "POST":
        nome = norm_turma(request.form.get("nome"))
        if not nome:
            flash("Informe o nome da turma.", "erro")
            return redirect(url_for("prof_turmas"))
        con.execute("INSERT INTO turmas(nome) VALUES (?) ON CONFLICT DO NOTHING", (nome,))
        con.commit()
        turma = con.execute("SELECT id FROM turmas WHERE nome=?", (nome,)).fetchone()
        n, u, i = importar_roster(turma["id"], request.form.get("lista"))
        flash("Turma %s: %d aluno(s) novo(s), %d atualizado(s), %d linha(s) ignorada(s)." % (nome, n, u, i), "ok")
        return redirect(url_for("prof_turma", tid=turma["id"]))
    turmas = con.execute(
        "SELECT t.id, t.nome, COUNT(a.id) AS n, COUNT(a.device_key) AS vinc "
        "FROM turmas t LEFT JOIN alunos a ON a.turma_id=t.id GROUP BY t.id ORDER BY t.nome").fetchall()
    return render_template("turmas.html", turmas=turmas)


@app.route("/prof/turmas/<int:tid>", methods=["GET", "POST"])
@login_required
def prof_turma(tid):
    con = db()
    turma = con.execute("SELECT * FROM turmas WHERE id=?", (tid,)).fetchone()
    if turma is None:
        abort(404)
    if request.method == "POST":
        n, u, i = importar_roster(tid, request.form.get("lista"))
        flash("%d novo(s), %d atualizado(s), %d linha(s) ignorada(s)." % (n, u, i), "ok")
        return redirect(url_for("prof_turma", tid=tid))
    alunos = con.execute("SELECT * FROM alunos WHERE turma_id=? ORDER BY LOWER(nome)", (tid,)).fetchall()
    return render_template("turma.html", turma=turma, alunos=alunos)


@app.post("/prof/alunos/<int:aid>/liberar")
@login_required
def prof_liberar(aid):
    con = db()
    al = con.execute("SELECT turma_id FROM alunos WHERE id=?", (aid,)).fetchone()
    if al is None:
        abort(404)
    con.execute("UPDATE alunos SET device_key=NULL, vinculado_em=NULL WHERE id=?", (aid,))
    con.commit()
    flash("Aparelho liberado. O aluno pode se cadastrar de novo no próximo escaneamento.", "ok")
    return redirect(url_for("prof_turma", tid=al["turma_id"]))


@app.post("/prof/alunos/<int:aid>/remover")
@login_required
def prof_remover_aluno(aid):
    con = db()
    al = con.execute("SELECT turma_id FROM alunos WHERE id=?", (aid,)).fetchone()
    if al is None:
        abort(404)
    con.execute("DELETE FROM leituras WHERE aluno_id=?", (aid,))
    con.execute("DELETE FROM saidas WHERE aluno_id=?", (aid,))
    con.execute("DELETE FROM alunos WHERE id=?", (aid,))
    con.commit()
    flash("Aluno removido.", "ok")
    return redirect(url_for("prof_turma", tid=al["turma_id"]))


# --------------------------------------------------------- professor: QR e relatório
@app.get("/prof/qr")
@login_required
def prof_qr():
    return render_template("qr.html", url=url_entrada())


def resumo_texto(e):
    a = e["aula"]
    linhas = ["Ata de frequência — %s — %s" % (a["turma"], a["disciplina"] or "sem disciplina informada"),
              "Data: %s | Início: %s | Tolerância: %d min | Corte: %s (até %s)" %
              (a["data"], a["inicio"], a["tolerancia_min"], a["corte_txt"], a["limite"]), ""]
    linhas.append("ATRASADOS (%d)" % len(e["atrasados"]))
    for x in e["atrasados"]:
        aut = {1: "autorização apresentada", 0: "SEM autorização", None: "autorização não conferida"}[x["autorizado"]]
        linhas.append("- %s (%s) — chegou %s (+%s) — %s" % (x["nome"], x["matricula"], x["chegada"], x["atraso_txt"], aut))
    linhas.append("")
    linhas.append("NÃO LERAM O QR CODE (%d)" % len(e["nao_leram"]))
    for x in e["nao_leram"]:
        linhas.append("- %s (%s)" % (x["nome"], x["matricula"]))
    linhas.append("")
    linhas.append("SAÍDAS ANTECIPADAS (%d)" % len(e["saidas"]))
    for x in e["saidas"]:
        linhas.append("- %s (%s) — %s%s" % (x["nome"], x["matricula"], x["hora"],
                                            (" — " + x["motivo"]) if x["motivo"] else ""))
    return "\n".join(linhas)


@app.get("/prof/aula/<int:aula_id>/relatorio")
@login_required
def prof_relatorio(aula_id):
    e = calcular_estado(aula_id)
    texto = resumo_texto(e)
    mailto = ""
    if PEDAGOGICO_EMAIL:
        assunto = "Frequência %s — %s — %s" % (e["aula"]["turma"], e["aula"]["data"], e["aula"]["disciplina"])
        mailto = "mailto:%s?subject=%s&body=%s" % (PEDAGOGICO_EMAIL, quote(assunto), quote(texto))
    return render_template("relatorio.html", e=e, texto=texto, mailto=mailto)


@app.get("/prof/aula/<int:aula_id>/relatorio.csv")
@login_required
def prof_relatorio_csv(aula_id):
    e = calcular_estado(aula_id)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["tipo", "matricula", "nome", "chegada", "atraso_min", "autorizacao", "observacao"])
    for x in e["atrasados"]:
        aut = {1: "apresentada", 0: "sem autorizacao", None: "nao conferida"}[x["autorizado"]]
        obs = "registro manual" if x["manual"] else ""
        w.writerow(["atrasado" if x["status"] == "atrasado" else "atraso acima do corte",
                    x["matricula"], x["nome"], x["chegada"], x["atraso_min"], aut, obs])
    for x in e["nao_leram"]:
        w.writerow(["nao leu QR", x["matricula"], x["nome"], "", "", "", ""])
    for x in e["saidas"]:
        w.writerow(["saida antecipada", x["matricula"], x["nome"], x["hora"], "", "", x["motivo"]])
    nome = "frequencia_%s_%s.csv" % (re.sub(r"[^A-Za-z0-9]+", "-", e["aula"]["turma"]),
                                      e["aula"]["data"].replace("/", "-"))
    return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % nome})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
