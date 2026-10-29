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
CREATE TABLE IF NOT EXISTS disciplinas(
  id {PK}, turma_id INTEGER NOT NULL REFERENCES turmas(id), nome TEXT NOT NULL,
  dias TEXT, data_ini TEXT, data_fim TEXT, encontros INTEGER, hora_inicio TEXT,
  UNIQUE(turma_id, nome, data_ini));
"""

# colunas acrescentadas depois da primeira versão (bancos já existentes são migrados)
COLUNAS_NOVAS = [
    ("turmas", "turno", "TEXT"),
    ("aulas", "disciplina_id", "INTEGER"),
    ("aulas", "numero", "INTEGER"),
    ("aulas", "data_ref", "TEXT"),
    ("aulas", "assunto", "TEXT"),
]


def _migrar_sqlite(con):
    for tabela, coluna, tipo in COLUNAS_NOVAS:
        existentes = {r[1] for r in con.execute("PRAGMA table_info(%s)" % tabela)}
        if coluna not in existentes:
            try:
                con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (tabela, coluna, tipo))
            except sqlite3.OperationalError:  # outro processo acrescentou ao mesmo tempo
                pass


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
        for tabela, coluna, tipo in COLUNAS_NOVAS:
            try:
                con.execute("ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s %s" % (tabela, coluna, tipo))
            except psycopg.Error:
                pass
        con.close()
        return
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    for stmt in _schema_sql():
        con.execute(stmt)
    _migrar_sqlite(con)
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


# ------------------------------------------------- disciplinas, agenda de aulas
FERIADOS = {
    "2026-01-01", "2026-04-03", "2026-04-21", "2026-05-01", "2026-09-07", "2026-10-12",
    "2026-11-02", "2026-11-15", "2026-11-20", "2026-12-25",
    "2027-01-01", "2027-03-26", "2027-04-21", "2027-05-01", "2027-09-07", "2027-10-12",
    "2027-11-02", "2027-11-15", "2027-11-20", "2027-12-25",
}
HORA_TURNO = {"manha": "07:30", "tarde": "13:30", "noite": "19:00"}
NOMES_DIA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def sem_acento(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")


def chave(s):
    return re.sub(r"[^a-z0-9]", "", sem_acento(s).lower())


def hoje():
    return datetime.fromtimestamp(now(), TZ).date()


def hora_do_turno(turno):
    return HORA_TURNO.get(chave(turno), "")


def parse_dias(texto):
    """'seg,qua,sex' ou o formato abreviado da planilha ('s,q,s' / 't,q,s')."""
    tokens = [chave(t) for t in re.split(r"[,;/\s]+", texto or "") if chave(t)]
    nomes = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "dom": 6}
    dias, anterior = [], None
    for t in tokens:
        if len(t) >= 3 and t[:3] in nomes:
            d = nomes[t[:3]]
        elif t == "s":
            d = 0 if anterior is None else 4
        elif t == "t":
            d = 1
        elif t == "q":
            d = 3 if anterior in (1, 2) else 2
        else:
            return []
        if d not in dias:
            dias.append(d)
        anterior = d
    return sorted(dias)


def dias_txt(dias):
    return ",".join(NOMES_DIA[d] for d in dias)


def parse_data(texto, padrao_ano=None, depois_de=None):
    t = (texto or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    try:
        if m:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", t)
        if not m:
            return None
        ano = int(m.group(3)) if m.group(3) else (padrao_ano or hoje().year)
        if ano < 100:
            ano += 2000
        d = datetime(ano, int(m.group(2)), int(m.group(1))).date()
        if not m.group(3) and depois_de and d < depois_de:
            d = datetime(ano + 1, d.month, d.day).date()
        return d
    except ValueError:
        return None


def gerar_datas(dias, ini, fim):
    out, d = [], ini
    while d <= fim and (d - ini).days <= 400:
        if d.weekday() in dias and d.isoformat() not in FERIADOS:
            out.append(d)
        d += timedelta(days=1)
    return out


def epoch_do_dia(data, hhmm):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", hhmm or "")
    h, mi = (int(m.group(1)), int(m.group(2))) if m else (19, 0)
    return int(datetime(data.year, data.month, data.day, h, mi, tzinfo=TZ).timestamp())


def renumerar(con, disciplina_id):
    linhas = con.execute("SELECT id FROM aulas WHERE disciplina_id=? ORDER BY data_ref, id",
                         (disciplina_id,)).fetchall()
    for i, r in enumerate(linhas, 1):
        con.execute("UPDATE aulas SET numero=? WHERE id=?", (i, r["id"]))


def obter_turma(con, nome, turno=None):
    nome = norm_turma(nome)
    con.execute("INSERT INTO turmas(nome) VALUES (?) ON CONFLICT DO NOTHING", (nome,))
    turma = con.execute("SELECT * FROM turmas WHERE nome=?", (nome,)).fetchone()
    if turno and not turma["turno"]:
        con.execute("UPDATE turmas SET turno=? WHERE id=?", (turno.strip()[:20], turma["id"]))
        turma = con.execute("SELECT * FROM turmas WHERE id=?", (turma["id"],)).fetchone()
    return turma


def salvar_disciplina(con, turma_nome, turno, dias_t, ini_t, fim_t, nome, encontros, hora=None):
    """Cria/atualiza a disciplina e gera as aulas (datas) que ainda não existem.
    Devolve (ok, mensagem)."""
    nome = norm_nome(nome)
    dias = parse_dias(dias_t)
    ini = parse_data(ini_t)
    fim = parse_data(fim_t, padrao_ano=ini.year if ini else None, depois_de=ini)
    if not norm_turma(turma_nome) or not nome:
        return False, "turma e disciplina são obrigatórias"
    if not dias:
        return False, "dias da semana não reconhecidos (%r)" % dias_t
    if ini is None or fim is None or fim < ini:
        return False, "datas inválidas (%s a %s)" % (ini_t, fim_t)
    try:
        enc = int(str(encontros).strip()) if str(encontros or "").strip() else None
    except ValueError:
        enc = None
    turma = obter_turma(con, turma_nome, turno)
    hora = (hora or "").strip() or hora_do_turno(turma["turno"] or turno) or "19:00"
    con.execute("INSERT INTO disciplinas(turma_id,nome,dias,data_ini,data_fim,encontros,hora_inicio) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (turma["id"], nome, dias_txt(dias), ini.isoformat(), fim.isoformat(), enc, hora))
    disc = con.execute("SELECT * FROM disciplinas WHERE turma_id=? AND nome=? AND data_ini=?",
                       (turma["id"], nome, ini.isoformat())).fetchone()
    con.execute("UPDATE disciplinas SET dias=?, data_fim=?, encontros=?, hora_inicio=? WHERE id=?",
                (dias_txt(dias), fim.isoformat(), enc, hora, disc["id"]))
    existentes = {r["data_ref"] for r in con.execute(
        "SELECT data_ref FROM aulas WHERE disciplina_id=?", (disc["id"],))}
    novas = 0
    for d in gerar_datas(dias, ini, fim):
        if d.isoformat() in existentes:
            continue
        con.execute("INSERT INTO aulas(turma_id,disciplina,inicio,status,criada_em,disciplina_id,data_ref) "
                    "VALUES (?,?,?,'agendada',?,?,?)",
                    (turma["id"], nome, epoch_do_dia(d, hora), now(), disc["id"], d.isoformat()))
        novas += 1
    renumerar(con, disc["id"])
    total = con.execute("SELECT COUNT(*) AS n FROM aulas WHERE disciplina_id=?", (disc["id"],)).fetchone()["n"]
    msg = "%s · %s: %d aula(s) criada(s), %d no total" % (turma["nome"], nome, novas, total)
    if enc and enc != total:
        msg += " (planilha informa %d encontro(s); ajuste na aba Aulas)" % enc
    return True, msg


def info_curso(con, aula):
    """Número da aula, quantas faltam e a data da prova (fim da disciplina)."""
    if not aula["disciplina_id"] or not aula["data_ref"]:
        return None
    disc = con.execute("SELECT * FROM disciplinas WHERE id=?", (aula["disciplina_id"],)).fetchone()
    total = con.execute("SELECT COUNT(*) AS n FROM aulas WHERE disciplina_id=?",
                        (aula["disciplina_id"],)).fetchone()["n"]
    faltam = con.execute("SELECT COUNT(*) AS n FROM aulas WHERE disciplina_id=? AND data_ref>?",
                         (aula["disciplina_id"], aula["data_ref"])).fetchone()["n"]
    fim = parse_data(disc["data_fim"]) if disc else None
    fim_txt = fim.strftime("%d/%m/%Y") if fim else ""
    if faltam > 0:
        texto = "Faltam %d aula%s para a prova%s." % (faltam, "" if faltam == 1 else "s",
                                                      " (%s)" % fim_txt if fim_txt else "")
    else:
        texto = "Esta é a última aula da disciplina — prova%s." % (" em %s" % fim_txt if fim_txt else "")
    return {"numero": aula["numero"], "total": total, "faltam": faltam, "fim": fim_txt,
            "texto": texto, "posicao": "Aula %s de %d" % (aula["numero"], total)}


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
    curso = info_curso(con, aula)
    return jsonify(ok=True, nova=nova, perfil=perfil, disciplina=aula["disciplina"] or "",
                   assunto=aula["assunto"] or "", curso=curso, data=dmy(aula["inicio"]),
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
    recentes = con.execute(base + "WHERE a.status='encerrada' ORDER BY a.inicio DESC LIMIT 15").fetchall()
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
    disciplina = (f.get("disciplina") or "").strip()[:120]
    con.execute("UPDATE aulas SET status='encerrada', encerrada_em=? WHERE turma_id=? AND status='aberta'",
                (t, turma_id))
    # se existe a aula do dia na agenda da turma, é ela que vale
    do_dia = None
    for cand in con.execute("SELECT * FROM aulas WHERE turma_id=? AND status='agendada' AND data_ref=? ORDER BY id",
                            (turma_id, hoje().isoformat())):
        if not disciplina or chave(cand["disciplina"]) == chave(disciplina):
            do_dia = cand
            break
    if do_dia is not None:
        con.execute("UPDATE aulas SET status='aberta', inicio=?, tolerancia_min=?, corte_min=?, criada_em=?, "
                    "encerrada_em=NULL WHERE id=?", (inicio, tol, corte, t, do_dia["id"]))
        aula_id = do_dia["id"]
    else:
        cur = con.execute(
            "INSERT INTO aulas(turma_id,disciplina,inicio,tolerancia_min,corte_min,criada_em,data_ref) "
            "VALUES (?,?,?,?,?,?,?)", (turma_id, disciplina, inicio, tol, corte, t, hoje().isoformat()))
        aula_id = cur.lastrowid
    con.commit()
    return redirect(url_for("prof_aula", aula_id=aula_id))


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

    nao_leram, atrasados, atencao, nao_assinam, entraram = [], [], [], [], []
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
                    autorizado=lt["autorizado"], manual=bool(lt["manual"]),
                    local=(None if lt["manual"] else lt["lat"] is not None))
        if st == "ok":
            no_horario += 1
            entraram.append(item)
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
    curso = info_curso(con, aula)
    return {
        "aula": {"id": aula["id"], "turma": aula["turma"], "disciplina": aula["disciplina"] or "",
                 "assunto": aula["assunto"] or "", "curso": curso,
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
        "entraram": entraram, "nao_assinam": nao_assinam,
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
    loc = {r["aluno_id"]: r for r in con.execute(
        "SELECT aluno_id, COUNT(*) AS n, SUM(CASE WHEN lat IS NOT NULL THEN 1 ELSE 0 END) AS com, "
        "SUM(fora) AS fora FROM leituras WHERE manual=0 AND aluno_id IN "
        "(SELECT id FROM alunos WHERE turma_id=?) GROUP BY aluno_id", (tid,))}
    return render_template("turma.html", turma=turma, alunos=alunos, loc=loc, geofence=GEOFENCE)


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
              (a["data"], a["inicio"], a["tolerancia_min"], a["corte_txt"], a["limite"])]
    if a.get("assunto"):
        linhas.append("Assunto: %s" % a["assunto"])
    if a.get("curso"):
        linhas.append("%s · %s" % (a["curso"]["posicao"], a["curso"]["texto"]))
    linhas.append("")
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


# ------------------------------------------------------------ professor: agenda
def ler_texto_enviado():
    """Texto colado no formulário ou arquivo enviado (csv/tsv/txt)."""
    texto = request.form.get("lista") or ""
    arq = request.files.get("arquivo")
    if arq and arq.filename:
        bruto = arq.read(256 * 1024)
        try:
            texto = bruto.decode("utf-8-sig")
        except UnicodeDecodeError:
            texto = bruto.decode("latin-1")
    return texto


def detectar_sep(linha):
    return "\t" if "\t" in linha else (";" if ";" in linha else ",")


def parse_tabela_disciplinas(texto, ignorar_finalizadas=True):
    """Aceita a aba 'Alocações' da planilha (com cabeçalho) ou colunas simples.
    Devolve (linhas, ignoradas); cada linha é um dict."""
    linhas = [l for l in (texto or "").splitlines() if l.strip()]
    if not linhas:
        return [], 0
    rows = list(csv.reader(linhas, delimiter=detectar_sep(linhas[0])))
    mapa = {}
    sinonimos = {"turma": "turma", "turno": "turno", "dsem": "dias", "dias": "dias", "dia": "dias",
                 "diasdasemana": "dias", "inicio": "ini", "fim": "fim", "materia": "nome",
                 "disciplina": "nome", "enc": "enc", "encontros": "enc", "hora": "hora",
                 "horario": "hora", "status": "status", "excluir": "excluir"}
    if chave(rows[0][0]) == "turma":
        for i, c in enumerate(rows[0]):
            k = sinonimos.get(chave(c))
            if k and k not in mapa:
                mapa[k] = i
        rows = rows[1:]
    elif len(rows[0]) >= 8:  # ordem da planilha, sem cabeçalho
        mapa = dict(turma=0, turno=1, dias=2, ini=3, fim=4, nome=6, enc=7)
    else:
        mapa = dict(turma=0, dias=1, ini=2, fim=3, nome=4, enc=5)
    saida, ignoradas = [], 0
    for r in rows:
        def cel(k):
            i = mapa.get(k)
            return r[i].strip() if i is not None and i < len(r) else ""
        if not cel("turma") or not cel("nome"):
            ignoradas += 1
            continue
        if cel("excluir") or (ignorar_finalizadas and chave(cel("status")) == "finalizado"):
            ignoradas += 1
            continue
        saida.append({k: cel(k) for k in ("turma", "turno", "dias", "ini", "fim", "nome", "enc", "hora")})
    return saida, ignoradas


def importar_disciplinas(texto, ignorar_finalizadas=True):
    con = db()
    linhas, ignoradas = parse_tabela_disciplinas(texto, ignorar_finalizadas)
    ok, avisos = 0, []
    for l in linhas:
        bom, msg = salvar_disciplina(con, l["turma"], l["turno"], l["dias"], l["ini"], l["fim"],
                                     l["nome"], l["enc"], l["hora"])
        if bom:
            ok += 1
        avisos.append(("" if bom else "ERRO — ") + msg)
    con.commit()
    return ok, ignoradas, avisos


def importar_alunos_multi(texto):
    """Linhas 'turma; matrícula; nome' (separador tab, ; ou ,)."""
    con = db()
    linhas = [l for l in (texto or "").splitlines() if l.strip()]
    novos = atualizados = ignoradas = 0
    if not linhas:
        return 0, 0, 0
    sep = detectar_sep(linhas[0])
    for r in csv.reader(linhas, delimiter=sep):
        if r and chave(r[0]) == "turma":  # cabeçalho
            continue
        if len(r) < 3:
            ignoradas += 1
            continue
        turma_nome, mat, nome = norm_turma(r[0]), norm_mat(r[1]), norm_nome(r[2])
        if not turma_nome or len(mat) < 4 or not nome:
            ignoradas += 1
            continue
        turma = obter_turma(con, turma_nome)
        ex = con.execute("SELECT id FROM alunos WHERE matricula=?", (mat,)).fetchone()
        if ex:
            con.execute("UPDATE alunos SET nome=?, turma_id=?, fora_lista=0 WHERE id=?",
                        (nome, turma["id"], ex["id"]))
            atualizados += 1
        else:
            con.execute("INSERT INTO alunos(matricula,nome,turma_id) VALUES (?,?,?)",
                        (mat, nome, turma["id"]))
            novos += 1
    con.commit()
    return novos, atualizados, ignoradas


@app.get("/prof/agenda")
@login_required
def prof_agenda():
    con = db()
    hj = hoje()
    turma_id = request.args.get("turma", type=int)
    todas = request.args.get("todas") == "1"
    turmas = con.execute("SELECT id, nome FROM turmas ORDER BY nome").fetchall()
    sql = ("SELECT d.*, t.nome AS turma, t.turno AS turno FROM disciplinas d "
           "JOIN turmas t ON t.id=d.turma_id WHERE 1=1 ")
    par = []
    if turma_id:
        sql += "AND d.turma_id=? "
        par.append(turma_id)
    if not todas:
        sql += "AND d.data_fim>=? "
        par.append((hj - timedelta(days=7)).isoformat())
    sql += "ORDER BY t.nome, d.data_ini, d.nome"
    discs = []
    for d in con.execute(sql, par).fetchall():
        aulas = []
        for a in con.execute(
                "SELECT a.*, (SELECT COUNT(*) FROM leituras l WHERE l.aula_id=a.id) AS leram "
                "FROM aulas a WHERE a.disciplina_id=? ORDER BY a.data_ref, a.id", (d["id"],)):
            dt = parse_data(a["data_ref"])
            aulas.append(dict(a, dia=("%s %s" % (NOMES_DIA[dt.weekday()], dt.strftime("%d/%m"))) if dt else "",
                              hoje=(a["data_ref"] == hj.isoformat())))
        fim, ini = parse_data(d["data_fim"]), parse_data(d["data_ini"])
        discs.append({"d": d, "aulas": aulas,
                      "periodo": "%s a %s" % (ini.strftime("%d/%m/%Y") if ini else "?",
                                              fim.strftime("%d/%m/%Y") if fim else "?"),
                      "em_curso": bool(ini and fim and ini <= hj <= fim)})
    de_hoje = con.execute(
        "SELECT a.*, t.nome AS turma, d.hora_inicio AS hora FROM aulas a JOIN turmas t ON t.id=a.turma_id "
        "LEFT JOIN disciplinas d ON d.id=a.disciplina_id WHERE a.data_ref=? AND a.status IN ('agendada','aberta') "
        "ORDER BY t.nome", (hj.isoformat(),)).fetchall()
    return render_template("agenda.html", discs=discs, turmas=turmas, turma_id=turma_id, todas=todas,
                           de_hoje=de_hoje, hm=hm, hoje=hj.isoformat(),
                           caminho=request.full_path.rstrip("?"))


@app.post("/prof/agenda/<int:aula_id>/abrir")
@login_required
def prof_agenda_abrir(aula_id):
    con = db()
    aula = con.execute("SELECT * FROM aulas WHERE id=?", (aula_id,)).fetchone()
    if aula is None or aula["status"] == "encerrada":
        abort(404)
    f = request.form
    try:
        tol = max(0, min(600, int(f.get("tolerancia") or TOLERANCIA_PADRAO)))
        corte = max(1, min(600, int(f.get("corte") or CORTE_PADRAO)))
    except ValueError:
        abort(400)
    t = now()
    inicio = parse_hm(f.get("inicio")) or t
    con.execute("UPDATE aulas SET status='encerrada', encerrada_em=? WHERE turma_id=? AND status='aberta' AND id<>?",
                (t, aula["turma_id"], aula_id))
    con.execute("UPDATE aulas SET status='aberta', inicio=?, tolerancia_min=?, corte_min=?, criada_em=?, "
                "encerrada_em=NULL WHERE id=?", (inicio, tol, corte, t, aula_id))
    con.commit()
    return redirect(url_for("prof_aula", aula_id=aula_id))


@app.post("/prof/agenda/<int:aula_id>/assunto")
@login_required
def prof_agenda_assunto(aula_id):
    con = db()
    if con.execute("SELECT 1 FROM aulas WHERE id=?", (aula_id,)).fetchone() is None:
        abort(404)
    con.execute("UPDATE aulas SET assunto=? WHERE id=?",
                ((request.form.get("assunto") or "").strip()[:300], aula_id))
    con.commit()
    flash("Assunto salvo.", "ok")
    return redirect(seguro_next(request.form.get("next")) + "#aula%d" % aula_id)


@app.post("/prof/aula/<int:aula_id>/assunto")
@login_required
def prof_aula_assunto(aula_id):
    get_aula(aula_id)
    d = request.get_json(silent=True) or {}
    con = db()
    con.execute("UPDATE aulas SET assunto=? WHERE id=?", ((d.get("assunto") or "").strip()[:300], aula_id))
    con.commit()
    return jsonify(ok=True)


@app.post("/prof/agenda/disciplina/<int:did>/adicionar")
@login_required
def prof_agenda_adicionar(did):
    con = db()
    disc = con.execute("SELECT * FROM disciplinas WHERE id=?", (did,)).fetchone()
    if disc is None:
        abort(404)
    d = parse_data(request.form.get("data"))
    if d is None:
        flash("Data inválida.", "erro")
    elif con.execute("SELECT 1 FROM aulas WHERE disciplina_id=? AND data_ref=?", (did, d.isoformat())).fetchone():
        flash("Já existe aula nessa data.", "erro")
    else:
        con.execute("INSERT INTO aulas(turma_id,disciplina,inicio,status,criada_em,disciplina_id,data_ref) "
                    "VALUES (?,?,?,'agendada',?,?,?)",
                    (disc["turma_id"], disc["nome"], epoch_do_dia(d, disc["hora_inicio"]), now(), did, d.isoformat()))
        renumerar(con, did)
        con.commit()
        flash("Aula de %s adicionada." % d.strftime("%d/%m/%Y"), "ok")
    return redirect(seguro_next(request.form.get("next")))


@app.post("/prof/agenda/<int:aula_id>/remover")
@login_required
def prof_agenda_remover(aula_id):
    con = db()
    aula = con.execute("SELECT * FROM aulas WHERE id=?", (aula_id,)).fetchone()
    if aula is None:
        abort(404)
    tem = con.execute("SELECT 1 FROM leituras WHERE aula_id=?", (aula_id,)).fetchone()
    if aula["status"] != "agendada" or tem:
        flash("Só dá para remover aulas ainda não abertas.", "erro")
    else:
        con.execute("DELETE FROM aulas WHERE id=?", (aula_id,))
        if aula["disciplina_id"]:
            renumerar(con, aula["disciplina_id"])
        con.commit()
        flash("Aula removida.", "ok")
    return redirect(seguro_next(request.form.get("next")))


# ------------------------------------------------------------ professor: importar
@app.get("/prof/importar")
@login_required
def prof_importar():
    turmas = db().execute("SELECT id, nome FROM turmas ORDER BY nome").fetchall()
    return render_template("importar.html", turmas=turmas)


@app.post("/prof/importar/<tipo>")
@login_required
def prof_importar_acao(tipo):
    con = db()
    if tipo == "turmas":
        n = 0
        for linha in ler_texto_enviado().splitlines():
            partes = [p.strip() for p in re.split(r"[;\t]", linha)]
            if partes and partes[0]:
                obter_turma(con, partes[0], partes[1] if len(partes) > 1 else None)
                n += 1
        con.commit()
        flash("%d turma(s) processada(s)." % n, "ok")
    elif tipo == "alunos":
        texto = ler_texto_enviado()
        if request.form.get("com_turma"):
            n, u, i = importar_alunos_multi(texto)
        else:
            nome = norm_turma(request.form.get("turma_nova")) or ""
            if nome:
                turma_id = obter_turma(con, nome)["id"]
                con.commit()
            else:
                turma_id = request.form.get("turma_id", type=int)
            if not turma_id or con.execute("SELECT 1 FROM turmas WHERE id=?", (turma_id,)).fetchone() is None:
                flash("Escolha ou informe a turma.", "erro")
                return redirect(url_for("prof_importar"))
            n, u, i = importar_roster(turma_id, texto)
        flash("Alunos: %d novo(s), %d atualizado(s), %d linha(s) ignorada(s)." % (n, u, i), "ok")
    elif tipo == "disciplinas":
        ok, ign, avisos = importar_disciplinas(ler_texto_enviado(), ignorar_finalizadas=bool(request.form.get("so_ativas")))
        flash("Disciplinas: %d importada(s), %d linha(s) ignorada(s)." % (ok, ign), "ok" if ok else "erro")
        for a in avisos[:30]:
            flash(a, "erro" if a.startswith("ERRO") else "ok")
    elif tipo == "manual":
        f = request.form
        ok, msg = salvar_disciplina(con, f.get("turma"), f.get("turno"), f.get("dias"), f.get("ini"),
                                    f.get("fim"), f.get("nome"), f.get("enc"), f.get("hora"))
        con.commit()
        flash(msg if ok else "Não foi possível: " + msg, "ok" if ok else "erro")
    else:
        abort(404)
    return redirect(url_for("prof_importar") if tipo != "disciplinas" else url_for("prof_agenda"))


@app.post("/prof/alunos/<int:aid>/editar")
@login_required
def prof_editar_aluno(aid):
    con = db()
    al = con.execute("SELECT * FROM alunos WHERE id=?", (aid,)).fetchone()
    if al is None:
        abort(404)
    nome, mat = norm_nome(request.form.get("nome")), norm_mat(request.form.get("matricula"))
    if not nome or len(mat) < 4:
        flash("Informe o nome e uma matrícula válida.", "erro")
    elif mat != al["matricula"] and con.execute("SELECT 1 FROM alunos WHERE matricula=?", (mat,)).fetchone():
        flash("Já existe outro aluno com a matrícula %s." % mat, "erro")
    else:
        con.execute("UPDATE alunos SET nome=?, matricula=? WHERE id=?", (nome, mat, aid))
        con.commit()
        flash("Aluno atualizado.", "ok")
    return redirect(url_for("prof_turma", tid=al["turma_id"]))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
