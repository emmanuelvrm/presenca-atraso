"""Teste do fluxo completo: python test_app.py"""
import os
import tempfile

TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
os.environ["PROF_SENHA"] = "senha-teste"
os.environ["SECRET_KEY"] = "x" * 32

import app as appmod  # noqa: E402

app = appmod.app
T0 = 1_800_000_000  # instante fixo
appmod.now = lambda: T0
H = {"X-Requested-With": "fetch"}


def post(c, url, **kw):
    return c.post(url, **kw)


def main():
    prof = app.test_client()
    # sem login
    assert prof.get("/prof").status_code == 302
    assert prof.get("/prof/aula/1/estado", headers=H).status_code == 401
    assert prof.post("/prof/login", data={"senha": "errada"}).status_code == 200
    assert prof.post("/prof/login", data={"senha": "senha-teste"}).status_code == 302

    # turma + lista (arquivo de exemplo com 17 alunos)
    lista = open(os.path.join(os.path.dirname(__file__), "exemplo_turma.txt")).read()
    r = prof.post("/prof/turmas", data={"nome": "inf10-m", "lista": lista}, follow_redirects=True)
    assert r.status_code == 200 and "17 aluno(s) novo(s)" in r.get_data(as_text=True)
    achados, ignoradas = appmod.parse_roster("Nome Solto\nXYZ123;Fulano\nSiclano;ABC456\n")
    assert achados == [("XYZ123", "Fulano"), ("ABC456", "Siclano")] and ignoradas == 1

    with app.app_context():
        con = appmod.db()
        turma_id = con.execute("SELECT id FROM turmas WHERE nome='INF10-M'").fetchone()["id"]

    # abre aula com início 40 min antes
    inicio_hm = appmod.hm(T0 - 40 * 60)
    r = prof.post("/prof/aulas", data={"turma_id": turma_id, "inicio": inicio_hm,
                                       "disciplina": "ASO II", "tolerancia": 10, "corte": 90})
    assert r.status_code == 302
    aula_id = int(r.headers["Location"].rsplit("/", 1)[1])

    def aluno_escaneia(mat, nome="Fulano", turma="INF10-M", client=None):
        c = client or app.test_client()
        rr = c.post("/api/registrar", json={"matricula": mat, "nome": nome, "turma": turma})
        return c, rr

    def checkin(c, agora):
        appmod.now = lambda: agora
        try:
            return c.post("/api/checkin", json={})
        finally:
            appmod.now = lambda: T0

    # QR
    qr = prof.get("/qr.png")
    assert qr.status_code == 200 and qr.data[:4] == b"\x89PNG"

    # aluno novo sem cadastro -> 401
    anon = app.test_client()
    assert anon.post("/api/checkin", json={}).status_code == 401

    # A: chega no horário (aula começou há 40 min, mas simulamos leitura em T0-40min+5)
    cA, rr = aluno_escaneia("INF240118")
    assert rr.status_code == 200 and rr.json["ok"], rr.json
    assert rr.json["aluno"]["nome"] == "Ana Souza"  # nome canônico do roster
    j = checkin(cA, T0 - 35 * 60).json
    assert j["ok"] and j["status"] == "ok" and j["nova"], j
    # segunda leitura é idempotente
    j2 = checkin(cA, T0).json
    assert not j2["nova"] and j2["chegada"] == j["chegada"]

    # B: atrasado 30 min
    cB, _ = aluno_escaneia("INF240136")
    j = checkin(cB, T0 - 10 * 60).json
    assert j["status"] == "atrasado" and j["atraso_min"] == 30, j

    # D: 100 min de atraso (bloqueado) -> usa outra aula? simulamos leitura em T0+60min
    cD, _ = aluno_escaneia("INF240149")
    j = checkin(cD, T0 + 60 * 60).json
    assert j["status"] == "bloqueado" and j["atraso_min"] == 100, j

    # vínculo de aparelho
    c2 = app.test_client()
    rr = c2.post("/api/registrar", json={"matricula": "INF240118", "nome": "x", "turma": "INF10-M"})
    assert rr.status_code == 409  # matrícula já vinculada a outro aparelho
    rr = cA.post("/api/registrar", json={"matricula": "INF240136", "nome": "x", "turma": "INF10-M"})
    assert rr.status_code == 409  # aparelho já vinculado a outro aluno

    # aluno fora da lista + nome com HTML
    cX, rr = aluno_escaneia("ZZ9999", nome="<b>Intruso</b>")
    assert rr.status_code == 200
    checkin(cX, T0 - 30 * 60)

    # estado do professor
    e = prof.get("/prof/aula/%d/estado" % aula_id, headers=H).json
    nomes_nl = {x["matricula"] for x in e["nao_leram"]}
    assert "INF240118" not in nomes_nl and "INF240123" in nomes_nl
    assert e["totais"]["turma"] == 18 and e["totais"]["no_horario"] >= 2
    atr = {x["matricula"]: x for x in e["atrasados"]}
    assert set(atr) == {"INF240136", "INF240149"}
    mot = {x["matricula"]: x["motivo"] for x in e["nao_assinam"]}
    assert "não conferida" in mot["INF240136"]
    assert "acima" in mot["INF240149"].lower()
    assert any(a["matricula"] == "ZZ9999" for a in e["atencao"])
    assert e["totais"]["a_conferir"] == 1

    idB = atr["INF240136"]["id"]
    # autorização
    assert prof.post("/prof/aula/%d/autorizacao" % aula_id, json={"aluno_id": idB, "valor": 1}, headers=H).json["ok"]
    e = prof.get("/prof/aula/%d/estado" % aula_id, headers=H).json
    assert "INF240136" not in {x["matricula"] for x in e["nao_assinam"]}
    assert prof.post("/prof/aula/%d/autorizacao" % aula_id, json={"aluno_id": idB, "valor": 0}, headers=H).json["ok"]
    e = prof.get("/prof/aula/%d/estado" % aula_id, headers=H).json
    assert {x["matricula"]: x["motivo"] for x in e["nao_assinam"]}["INF240136"] == "Atrasado, sem autorização"
    prof.post("/prof/aula/%d/autorizacao" % aula_id, json={"aluno_id": idB, "valor": 1}, headers=H)

    # registro manual + saída
    idK = next(x["id"] for x in e["nao_leram"] if x["matricula"] == "INF240123")
    assert prof.post("/prof/aula/%d/manual" % aula_id, json={"aluno_id": idK}, headers=H).json["ok"]
    assert prof.post("/prof/aula/%d/saida" % aula_id, json={"aluno_id": idK, "motivo": "médico"}, headers=H).json["ok"]
    e = prof.get("/prof/aula/%d/estado" % aula_id, headers=H).json
    assert len(e["saidas"]) == 1 and "INF240123" not in {x["matricula"] for x in e["nao_leram"]}

    # liberar aparelho e recadastrar
    with app.app_context():
        aid = appmod.db().execute("SELECT id FROM alunos WHERE matricula='INF240118'").fetchone()["id"]
    assert prof.post("/prof/alunos/%d/liberar" % aid).status_code == 302
    rr = c2.post("/api/registrar", json={"matricula": "INF240118", "nome": "x", "turma": "INF10-M"})
    assert rr.status_code == 200

    # relatório, CSV (sem HTML cru)
    r = prof.get("/prof/aula/%d/relatorio" % aula_id)
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "<b>Intruso</b>" not in html and "médico" in html
    csvr = prof.get("/prof/aula/%d/relatorio.csv" % aula_id)
    assert csvr.status_code == 200 and b"saida antecipada" in csvr.data
    for url in ("/prof", "/prof/turmas", "/prof/turmas/%d" % turma_id, "/prof/qr", "/prof/aula/%d" % aula_id):
        assert prof.get(url).status_code == 200, url

    # encerrar -> alunos sem aula aberta
    assert prof.post("/prof/aula/%d/encerrar" % aula_id).status_code == 302
    assert checkin(cA, T0).status_code == 401  # aparelho antigo foi liberado
    j = checkin(c2, T0).json
    assert j["ok"] is False and "Nenhuma aula aberta" in j["msg"]
    print("TODOS OS TESTES PASSARAM")


if __name__ == "__main__":
    main()
