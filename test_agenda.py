"""Teste da agenda de disciplinas/aulas, importação, assunto, edição de aluno e tag de local."""
import os
import tempfile
from datetime import datetime

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["PROF_SENHA"] = "senha-teste"
os.environ["SECRET_KEY"] = "y" * 32

import app as appmod  # noqa: E402

app = appmod.app
TZ = appmod.TZ


def em(d, m, h=19, mi=0, ano=2026):
    return int(datetime(ano, m, d, h, mi, tzinfo=TZ).timestamp())


PLANILHA = (
    "Turma\tTurno\tD. Sem\tInicio\tFim\tEncontros restantes\tMateria\tEnc.\tFat\tStatus\n"
    "INF12T\tTarde\ts,q,s\t28/10\t30/11\t12\tLinguagem de Programação para Web\t13\t1227,20\t>>>\n"
    "INF13N\tNoite\tt,q,s\t22/10\t07/11\t5\tEstrutura de Dados\t8\t755,20\t>>>\n"
    "INF08M\tManhã\ts,q,s\t21/09\t26/10\t0\tAdministração de Sistemas Operacionais II\t15\t1416,00\tFinalizado\n"
)


def main():
    # dias da semana
    assert appmod.parse_dias("s,q,s") == [0, 2, 4]
    assert appmod.parse_dias("t,q,s") == [1, 3, 4]
    assert appmod.parse_dias("t,q") == [1, 3]
    assert appmod.parse_dias("seg, qua") == [0, 2]
    assert appmod.parse_dias("Terça;Quinta") == [1, 3]
    assert appmod.parse_dias("xyz") == []

    appmod.now = lambda: em(29, 10)
    prof = app.test_client()
    assert prof.post("/prof/login", data={"senha": "senha-teste"}).status_code == 302
    for url in ("/prof/agenda", "/prof/importar"):
        assert prof.get(url).status_code == 200, url
    assert app.test_client().get("/prof/agenda").status_code == 302

    # importa a planilha (pula Finalizado)
    r = prof.post("/prof/importar/disciplinas", data={"lista": PLANILHA, "so_ativas": "1"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "2 importada(s), 1 linha(s) ignorada(s)" in html, html[:2000]
    assert "Linguagem de Programação para Web" in html
    con = appmod.db  # noqa: F841
    with app.app_context():
        c = appmod.db()
        web = c.execute("SELECT * FROM disciplinas WHERE nome LIKE 'Linguagem%'").fetchone()
        n_web = c.execute("SELECT COUNT(*) AS n FROM aulas WHERE disciplina_id=?", (web["id"],)).fetchone()["n"]
        ed = c.execute("SELECT * FROM disciplinas WHERE nome LIKE 'Estrutura%'").fetchone()
        n_ed = c.execute("SELECT COUNT(*) AS n FROM aulas WHERE disciplina_id=?", (ed["id"],)).fetchone()["n"]
        assert n_web == 13 and n_ed == 8, (n_web, n_ed)  # feriados 02/11 e 20/11 pulados
        assert c.execute("SELECT COUNT(*) AS n FROM disciplinas").fetchone()["n"] == 2
        assert c.execute("SELECT turno FROM turmas WHERE nome='INF12T'").fetchone()["turno"] == "Tarde"
        aula30 = c.execute("SELECT * FROM aulas WHERE disciplina_id=? AND data_ref='2026-10-30'",
                           (web["id"],)).fetchone()
        assert aula30["numero"] == 2 and aula30["status"] == "agendada"
        datas_web = [r["data_ref"] for r in c.execute(
            "SELECT data_ref FROM aulas WHERE disciplina_id=? ORDER BY data_ref", (web["id"],))]
        assert "2026-11-02" not in datas_web and "2026-11-20" not in datas_web
    # reimportar não duplica
    prof.post("/prof/importar/disciplinas", data={"lista": PLANILHA, "so_ativas": "1"})
    with app.app_context():
        assert appmod.db().execute("SELECT COUNT(*) AS n FROM aulas").fetchone()["n"] == 21

    # alunos de várias turmas de uma vez + importação simples
    r = prof.post("/prof/importar/alunos", data={
        "com_turma": "1",
        "lista": "Turma;Matrícula;Nome\nINF12T;INF260001;Bruno Alves\nINF12T;INF260002;Carla Dias\nINF13N;INF260003;Davi Lima\n"},
        follow_redirects=True)
    assert "3 novo(s)" in r.get_data(as_text=True)
    r = prof.post("/prof/importar/alunos", data={"turma_nova": "INF99X", "lista": "INF260010 Elisa Prado\n"},
                  follow_redirects=True)
    assert "1 novo(s)" in r.get_data(as_text=True)
    r = prof.post("/prof/importar/turmas", data={"lista": "INF77Z;Manhã\n"}, follow_redirects=True)
    assert "1 turma(s)" in r.get_data(as_text=True)
    r = prof.post("/prof/importar/manual", data={"turma": "INF77Z", "turno": "Manhã", "nome": "Rede de Computadores",
                                                  "dias": "seg,qua", "ini": "02/11/2026", "fim": "11/11/2026", "enc": "4"},
                  follow_redirects=True)
    assert "aula(s) criada(s)" in r.get_data(as_text=True)
    r = prof.post("/prof/importar/manual", data={"turma": "X", "nome": "Y", "dias": "??", "ini": "01/01", "fim": "02/01"},
                  follow_redirects=True)
    assert "Não foi possível" in r.get_data(as_text=True)

    # assunto pela agenda
    r = prof.post("/prof/agenda/%d/assunto" % aula30["id"],
                  data={"assunto": "Formulários em Flask <b>x</b>", "next": "/prof/agenda"}, follow_redirects=True)
    assert r.status_code == 200 and "<b>x</b>" not in r.get_data(as_text=True)

    # aluno se cadastra e escaneia em 30/10 às 13:35 (aula 13:30)
    appmod.now = lambda: em(30, 10, 13, 35)
    # abre pela aula do dia (formulário ad hoc reaproveita a agendada)
    with app.app_context():
        tid = appmod.db().execute("SELECT id FROM turmas WHERE nome='INF12T'").fetchone()["id"]
    r = prof.post("/prof/aulas", data={"turma_id": tid, "inicio": "13:30", "disciplina": "", "tolerancia": 10, "corte": 90})
    assert r.status_code == 302 and r.headers["Location"].endswith("/prof/aula/%d" % aula30["id"]), r.headers["Location"]
    with app.app_context():
        assert appmod.db().execute("SELECT COUNT(*) AS n FROM aulas").fetchone()["n"] > 0
        assert appmod.db().execute("SELECT status FROM aulas WHERE id=?", (aula30["id"],)).fetchone()["status"] == "aberta"

    b = app.test_client()
    assert b.post("/api/registrar", json={"matricula": "INF260001", "nome": "x", "turma": "INF12T"}).status_code == 200
    j = b.post("/api/checkin", json={"geo": {"lat": -19.8, "lng": -43.9}}).json
    assert j["ok"] and j["status"] == "ok", j
    assert j["assunto"].startswith("Formulários em Flask")
    assert j["curso"]["posicao"] == "Aula 2 de 13" and j["curso"]["faltam"] == 11, j["curso"]
    assert "Faltam 11 aulas para a prova" in j["curso"]["texto"] and "30/11/2026" in j["curso"]["texto"]
    c2 = app.test_client()
    assert c2.post("/api/registrar", json={"matricula": "INF260002", "nome": "x", "turma": "INF12T"}).status_code == 200
    j2 = c2.post("/api/checkin", json={"geo": None}).json  # sem localização
    assert j2["ok"]

    # estado: coluna "já entraram" + local
    e = prof.get("/prof/aula/%d/estado" % aula30["id"], headers={"X-Requested-With": "fetch"}).json
    assert {x["matricula"]: x["local"] for x in e["entraram"]} == {"INF260001": True, "INF260002": False}
    assert e["aula"]["assunto"].startswith("Formulários") and e["aula"]["curso"]["numero"] == 2
    # assunto pela tela da aula
    assert prof.post("/prof/aula/%d/assunto" % aula30["id"], json={"assunto": "Revisão"},
                     headers={"X-Requested-With": "fetch"}).json["ok"]
    assert b.post("/api/checkin", json={}).json["assunto"] == "Revisão"
    txt = prof.get("/prof/aula/%d/relatorio" % aula30["id"]).get_data(as_text=True)
    assert "Revisão" in txt and "Aula 2 de 13" in txt

    # tag de local na turma + edição do aluno
    t = prof.get("/prof/turmas/%d" % tid).get_data(as_text=True)
    assert "local capturado" in t and "sem local" in t
    with app.app_context():
        aid = appmod.db().execute("SELECT id FROM alunos WHERE matricula='INF260001'").fetchone()["id"]
    r = prof.post("/prof/alunos/%d/editar" % aid, data={"nome": "Bruno Alves Jr", "matricula": "inf260099"},
                  follow_redirects=True)
    assert "Aluno atualizado" in r.get_data(as_text=True) and "INF260099" in r.get_data(as_text=True)
    r = prof.post("/prof/alunos/%d/editar" % aid, data={"nome": "x", "matricula": "INF260002"}, follow_redirects=True)
    assert "Já existe outro aluno" in r.get_data(as_text=True)
    # o aparelho continua vinculado ao mesmo aluno depois de editar
    assert b.post("/api/checkin", json={}).json["perfil"]["matricula"] == "INF260099"

    # agenda: abrir uma aula específica pela aba, encerrar a anterior da mesma turma
    appmod.now = lambda: em(3, 11, 19, 5)
    with app.app_context():
        ed_aula = appmod.db().execute("SELECT id FROM aulas WHERE disciplina_id=? AND data_ref='2026-11-03'",
                                      (ed["id"],)).fetchone()["id"]
    r = prof.post("/prof/agenda/%d/abrir" % ed_aula, data={"inicio": "19:00"})
    assert r.status_code == 302
    d3 = app.test_client()
    assert d3.post("/api/registrar", json={"matricula": "INF260003", "nome": "x", "turma": "INF13N"}).status_code == 200
    j3 = d3.post("/api/checkin", json={}).json
    assert j3["ok"] and j3["curso"]["posicao"].startswith("Aula ") and j3["disciplina"] == "Estrutura de Dados"
    # remover aula só se agendada; adicionar data
    r = prof.post("/prof/agenda/%d/remover" % ed_aula, data={"next": "/prof/agenda"}, follow_redirects=True)
    assert "Só dá para remover" in r.get_data(as_text=True)
    r = prof.post("/prof/agenda/disciplina/%d/adicionar" % ed["id"], data={"data": "09/11/2026", "next": "/prof/agenda"},
                  follow_redirects=True)
    assert "adicionada" in r.get_data(as_text=True)
    with app.app_context():
        assert appmod.db().execute("SELECT COUNT(*) AS n FROM aulas WHERE disciplina_id=?", (ed["id"],)).fetchone()["n"] == 9
    # páginas
    for url in ("/prof/agenda", "/prof/agenda?todas=1", "/prof/agenda?turma=%d" % tid, "/prof/importar", "/prof"):
        assert prof.get(url).status_code == 200, url
    print("TESTE DE AGENDA PASSOU")


if __name__ == "__main__":
    main()
