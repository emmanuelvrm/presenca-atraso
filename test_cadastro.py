"""Teste de turmas (catálogo/massa), limpeza de alunos, disciplinas/cronograma, OCR (sem EasyOCR) e menu."""
import os
import tempfile
from datetime import datetime

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["PROF_SENHA"] = "senha-teste"
os.environ["SECRET_KEY"] = "z" * 32

import app as appmod  # noqa: E402
import grade_ocr  # noqa: E402

app = appmod.app
TZ = appmod.TZ
appmod.now = lambda: int(datetime(2026, 10, 29, 10, 0, tzinfo=TZ).timestamp())


def main():
    # --- limpeza de alunos
    achados, ign = appmod.parse_roster(
        "INF250005-Alonsio Andrade Da Silva\n"
        "INF240118Amanda Cristina Gomes Ferreira\n"
        "INF240136 Andre Lucas Pantaleao De Souza\n"
        "3) INF240140 – João D'Ávila Jr.\n"
        "  INF240141;;; Maria   da  Penha 123 \n"
        "Carlos Souza;INF240142\n"
        "linha sem matricula\n")
    assert achados == [
        ("INF250005", "Alonsio Andrade Da Silva"), ("INF240118", "Amanda Cristina Gomes Ferreira"),
        ("INF240136", "Andre Lucas Pantaleao De Souza"), ("INF240140", "João D'Ávila Jr"),
        ("INF240141", "Maria da Penha"), ("INF240142", "Carlos Souza")], achados
    assert ign == 1

    prof = app.test_client()
    assert prof.post("/prof/login", data={"senha": "senha-teste"}).status_code == 302

    # --- turmas: dropdown do catálogo, criação, massa
    html = prof.get("/prof/turmas").get_data(as_text=True)
    assert 'id="sel-turma"' in html and "INF10M · INF · Manhã" in html and "DSI01N" in html
    r = prof.post("/prof/turmas", data={"catalogo": "INF10M", "nome": "", "lista": "INF250005-Alonsio Andrade Da Silva\nINF240118Amanda Gomes"},
                  follow_redirects=True)
    assert "2 aluno(s) novo(s)" in r.get_data(as_text=True)
    with app.app_context():
        t = appmod.db().execute("SELECT * FROM turmas WHERE nome='INF10M'").fetchone()
        assert t["turno"] == "Manhã"
    assert "(já cadastrada)" in prof.get("/prof/turmas").get_data(as_text=True)
    r = prof.post("/prof/turmas/massa", data={"nomes": "INF12T\nINF13N;Noite\n\nINF10M\n"}, follow_redirects=True)
    assert "2 turma(s) criada(s), 1 já existia" in r.get_data(as_text=True)
    r = prof.post("/prof/turmas/massa", data={"todas_catalogo": "1"}, follow_redirects=True)
    with app.app_context():
        assert appmod.db().execute("SELECT COUNT(*) AS n FROM turmas").fetchone()["n"] == 20

    # --- disciplina com cronograma
    html = prof.get("/prof/disciplinas").get_data(as_text=True)
    assert "Nova disciplina" in html and "Banco de Dados" in html
    with app.app_context():
        tid = appmod.db().execute("SELECT id FROM turmas WHERE nome='INF10M'").fetchone()["id"]
    r = prof.post("/prof/disciplinas", data={
        "turma_id": tid, "nome": "Estrutura de Dados", "ini": "03/11/2026", "fim": "19/11/2026", "dias": "ter,qui",
        "hora": "07:30", "enc": 8,
        "cronograma": "Aula 1: Apresentação\nAula 2 - Listas\n3) Pilhas\nFilas\nAula 20: fora"}, follow_redirects=True)
    h = r.get_data(as_text=True)
    assert r.status_code == 200 and "Aula 1" in h and "Apresentação" in h and "Ignorada(s) aula(s) 20" in h, h[:3000]
    with app.app_context():
        con = appmod.db()
        d = con.execute("SELECT * FROM disciplinas WHERE nome='Estrutura de Dados'").fetchone()
        aulas = con.execute("SELECT numero, data_ref, assunto FROM aulas WHERE disciplina_id=? ORDER BY numero", (d["id"],)).fetchall()
        # 3/11 a 19/11, ter/qui = 3,5,10,12,17,19 (15/11 é feriado, sábado... não afeta)
        assert [a["data_ref"] for a in aulas] == ["2026-11-03", "2026-11-05", "2026-11-10", "2026-11-12", "2026-11-17", "2026-11-19"]
        assert [a["assunto"] for a in aulas[:4]] == ["Apresentação", "Listas", "Pilhas", "Filas"]
        did = d["id"]
        ids = [a["numero"] for a in aulas]
    assert prof.get("/prof/disciplinas/%d" % did).status_code == 200
    # salvar assuntos por campo e por texto
    with app.app_context():
        a6 = appmod.db().execute("SELECT id FROM aulas WHERE disciplina_id=? AND numero=6", (did,)).fetchone()["id"]
    r = prof.post("/prof/disciplinas/%d/cronograma" % did, data={"assunto-%d" % a6: "Prova prática", "cronograma": "Aula 5: Revisão"},
                  follow_redirects=True)
    assert "assunto(s) salvo(s)" in r.get_data(as_text=True)
    with app.app_context():
        rows = {r["numero"]: r["assunto"] for r in appmod.db().execute("SELECT numero, assunto FROM aulas WHERE disciplina_id=?", (did,))}
        assert rows[6] == "Prova prática" and rows[5] == "Revisão"
    # mudar período: sai o que ficou fora, entra o que faltava
    r = prof.post("/prof/disciplinas/%d/editar" % did, data={"nome": "Estrutura de Dados", "ini": "03/11/2026", "fim": "12/11/2026",
                                                           "dias": "ter,qui", "hora": "08:00", "enc": 8}, follow_redirects=True)
    assert "2 removida(s)" in r.get_data(as_text=True)
    with app.app_context():
        n = appmod.db().execute("SELECT COUNT(*) AS n FROM aulas WHERE disciplina_id=?", (did,)).fetchone()["n"]
        assert n == 4
    # excluir
    r = prof.post("/prof/disciplinas/%d/excluir" % did, follow_redirects=True)
    assert "Disciplina excluída" in r.get_data(as_text=True)

    # --- menu lateral e cards clicáveis
    html = prof.get("/prof").get_data(as_text=True)
    assert 'id="side"' in html and 'id="side-toggle"' in html and "Disciplinas" in html and 'class="nav"' not in html
    assert "Nenhuma aula cadastrada para hoje" in html

    # --- OCR: agrupamento, interpretação e rota (sem EasyOCR instalado)
    blocos = [([[100, 10], [200, 10], [200, 30], [100, 30]], "Banco de Dados", 0.9),
              ([[10, 12], [60, 12], [60, 32], [10, 32]], "INF12T", 0.9),
              ([[300, 11], [380, 11], [380, 31], [300, 31]], "03/11 a 19/11", 0.9),
              ([[10, 60], [60, 60], [60, 80], [10, 80]], "Redes", 0.9)]
    assert grade_ocr.agrupar_linhas(blocos) == ["INF12T  Banco de Dados  03/11 a 19/11", "Redes"]
    txt = "Ter/Qui  Banco de Dados  03/11/2026 a 19/11/2026  8 aulas\nlixo\nSegunda e quarta Redes de Computadores 05/11 - 30/11"
    lin = grade_ocr.interpretar(txt, "INF12T", "Tarde").splitlines()
    assert lin[0].split("\t") == ["INF12T", "Tarde", "ter,qui", "03/11/2026", "19/11/2026", "", "Banco de Dados", "8"], lin
    assert lin[1].split("\t")[2] == "seg,qua" and lin[1].split("\t")[6] == "Redes de Computadores", lin
    try:
        grade_ocr.extrair_texto(b"\x89PNG....", "g.png")
        raise SystemExit("deveria exigir easyocr")
    except grade_ocr.OCRIndisponivel:
        pass
    r = prof.post("/prof/importar/grade", data={"turma": "INF12T"}, follow_redirects=True)
    assert "Escolha uma imagem" in r.get_data(as_text=True)
    import io
    r = prof.post("/prof/importar/grade", data={"turma": "INF12T", "arquivo": (io.BytesIO(b"\x89PNG..."), "grade.png")},
                  content_type="multipart/form-data", follow_redirects=True)
    assert "EasyOCR não está instalado" in r.get_data(as_text=True)
    grade_ocr.extrair_texto = lambda dados, nome: (txt, "teste")  # simula a leitura
    r = prof.post("/prof/importar/grade", data={"turma": "INF12T", "arquivo": (io.BytesIO(b"x"), "grade.png")},
                  content_type="multipart/form-data")
    h = r.get_data(as_text=True)
    assert r.status_code == 200 and "Linhas para importar (2)" in h and "Banco de Dados" in h
    # as linhas interpretadas entram pelo importador de disciplinas
    r = prof.post("/prof/importar/disciplinas", data={"lista": "\n".join(lin)}, follow_redirects=True)
    assert "2 importada(s)" in r.get_data(as_text=True), r.get_data(as_text=True)[:2500]
    print("TESTE DE CADASTRO PASSOU")


if __name__ == "__main__":
    main()
