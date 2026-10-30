"""Teste: calendário/dias/fim automático, local válido/inválido, Wi-Fi/IP da escola, aviso do pedágio."""
import os
import tempfile
from datetime import datetime

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["PROF_SENHA"] = "senha-teste"
os.environ["SECRET_KEY"] = "y" * 32

import app as appmod  # noqa: E402

app, TZ = appmod.app, appmod.TZ
T0 = int(datetime(2026, 10, 29, 19, 0, tzinfo=TZ).timestamp())
appmod.now = lambda: T0


def main():
    # fim automático pelos encontros (pula feriado 02/11 e 15/11)
    d = appmod.parse_data
    assert appmod.fim_por_encontros([0, 2], d("2026-10-26"), 4) == d("2026-11-09"), appmod.fim_por_encontros([0, 2], d("2026-10-26"), 4)
    assert appmod.fim_por_encontros([], d("2026-10-26"), 4) is None

    prof = app.test_client()
    prof.post("/prof/login", data={"senha": "senha-teste"})
    prof.post("/prof/turmas", data={"nome": "INF10-M", "lista": "INF240118 Ana Souza\nINF240136 Bruno Lima"})
    with app.app_context():
        tid = appmod.db().execute("SELECT id FROM turmas WHERE nome='INF10-M'").fetchone()["id"]

    # formulário com calendário, flags de dias e fim vazio (calculado no servidor)
    html = prof.get("/prof/disciplinas").get_data(as_text=True)
    assert 'type="date"' in html and html.count('name="dias" value=') == 6 and "data-periodo" in html
    assert "2026-11-20" in html  # feriados para o JS
    r = prof.post("/prof/disciplinas", data={"turma_id": tid, "nome": "Redes", "ini": "2026-10-26", "fim": "",
                                              "dias": ["seg", "qua"], "enc": "4", "hora": "19:00"}, follow_redirects=True)
    assert "4 no total" in r.get_data(as_text=True), r.get_data(as_text=True)[:2000]
    with app.app_context():
        disc = appmod.db().execute("SELECT * FROM disciplinas WHERE nome='Redes'").fetchone()
        assert disc["data_fim"] == "2026-11-09" and disc["dias"] == "seg,qua", dict(disc)
    pag = prof.get("/prof/disciplinas/%d" % disc["id"]).get_data(as_text=True)
    assert 'value="2026-10-26"' in pag and 'value="2026-11-09"' in pag and 'value="seg" checked' in pag
    # edição pelos checkboxes
    r = prof.post("/prof/disciplinas/%d/editar" % disc["id"], data={
        "nome": "Redes", "ini": "2026-10-26", "fim": "", "dias": ["seg", "qua", "sex"], "enc": "6", "hora": "19:00"})
    with app.app_context():
        assert appmod.db().execute("SELECT data_fim FROM disciplinas WHERE id=?", (disc["id"],)).fetchone()["data_fim"] == "2026-11-09"

    # aula de hoje (29/10 é quinta) para testar leitura
    with app.app_context():
        con = appmod.db()
        ok, msg = appmod.salvar_disciplina(con, "INF10-M", "Noite", "qui", "2026-10-29", "", "Linux", 1, "19:00")
        assert ok, msg
        aid = con.execute("SELECT id FROM aulas WHERE disciplina='Linux'").fetchone()["id"]
        con.commit()
    prof.post("/prof/agenda/%d/assunto" % aid, data={"assunto": "Permissões", "next": "/prof"})
    assert prof.post("/prof/agenda/%d/abrir" % aid, data={"inicio": "19:00", "tolerancia": 10, "corte": 90}).status_code == 302

    # Wi-Fi: registra IP do professor (header do proxy) como da escola
    H = {"X-Forwarded-For": "200.1.2.3"}
    assert "não registrado" in prof.get("/prof/unidade", headers=H).get_data(as_text=True)
    prof.post("/prof/unidade/rede", data={"rotulo": "Grau Alunos"}, headers=H)
    assert "já é da escola" in prof.get("/prof/unidade", headers=H).get_data(as_text=True)

    def aluno(mat, ip, geo, atraso_s=0):
        c = app.test_client()
        c.post("/api/registrar", json={"matricula": mat, "nome": "x", "turma": "INF10-M"})
        appmod.now = lambda: T0 + atraso_s
        try:
            return c.post("/api/checkin", json={"geo": geo}, headers={"X-Forwarded-For": ip}).json
        finally:
            appmod.now = lambda: T0

    with app.app_context():
        lat, lng, _, _ = appmod.unidade()
    # A: no Wi-Fi da escola e GPS na escola -> tudo válido, sem pedágio (status ok)
    j = aluno("INF240118", "200.1.2.3", {"lat": lat, "lng": lng})
    assert j["local"] == "valido" and j["wifi"] is True and j["status"] == "ok", j
    # B: GPS a ~3 km, fora do Wi-Fi, atrasado 30 min -> inválido, sem wifi, status atrasado (pedágio)
    j = aluno("INF240136", "9.9.9.9", {"lat": lat + 0.03, "lng": lng}, 30 * 60)
    assert j["local"] == "invalido" and j["wifi"] is False and j["status"] == "atrasado", j

    est = prof.get("/prof/aula/%d/estado" % aid).json
    b = [x for x in est["atrasados"] if x["matricula"] == "INF240136"][0]
    assert b["local_estado"] == "invalido" and b["dist_m"] > 2000 and b["wifi"] is False and b["ip"] == "9.9.9.9"
    assert any("INVÁLIDO" in f for x in est["atencao"] for f in x["flags"])
    a = [x for x in est["entraram"] if x["matricula"] == "INF240118"][0]
    assert a["local_estado"] == "valido" and a["wifi"] is True and not est["atencao"][0:0]

    # professor valida / recusa
    assert prof.post("/prof/aula/%d/local" % aid, json={"aluno_id": b["id"], "valor": 1}).json["ok"]
    est = prof.get("/prof/aula/%d/estado" % aid).json
    assert [x for x in est["atrasados"] if x["id"] == b["id"]][0]["local_estado"] == "validado"
    prof.post("/prof/aula/%d/local" % aid, json={"aluno_id": b["id"], "valor": 0})
    est = prof.get("/prof/aula/%d/estado" % aid).json
    assert [x for x in est["atrasados"] if x["id"] == b["id"]][0]["local_estado"] == "recusado"
    assert prof.post("/prof/aula/%d/local" % aid, json={"aluno_id": b["id"], "valor": 5}).status_code == 400

    # GPS ajustado pelo professor
    assert prof.post("/prof/unidade/local", json={"lat": -19.8, "lng": -43.95, "raio": 150}).json["ok"]
    with app.app_context():
        assert appmod.unidade()[:3] == (-19.8, -43.95, 150.0)
    assert prof.post("/prof/unidade/local", json={"lat": 999, "lng": 0}).status_code == 400
    # protegido por login
    assert app.test_client().get("/prof/unidade").status_code == 302
    # tela do aluno traz o aviso do pedágio e os selos
    pagina = app.test_client().get("/e").get_data(as_text=True)
    assert "PASSE NO PEDÁGIO" in pagina and 'id="ck-wifi"' in pagina
    print("TESTE EXTRA PASSOU")


main()
