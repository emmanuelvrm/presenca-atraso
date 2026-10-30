"""Leitura de grade (imagem ou PDF) → texto → linhas no formato de importação de disciplinas.

O EasyOCR é pesado (PyTorch) e por isso OPCIONAL: só é importado quando alguém envia uma imagem.
Instale com `pip install -r requirements-ocr.txt`. Também funciona na linha de comando, no seu
computador, para gerar o texto e colar em Importar → Disciplinas:

    python grade_ocr.py grade.png INF12T
"""
import re
import sys
import unicodedata

_LEITOR = None


class OCRIndisponivel(Exception):
    pass


def _sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)).lower()


def _leitor():
    global _LEITOR
    if _LEITOR is None:
        try:
            import easyocr
        except ImportError as e:
            raise OCRIndisponivel(
                "O EasyOCR não está instalado neste servidor (ele é pesado e não cabe no plano gratuito). "
                "Rode `python grade_ocr.py grade.png TURMA` no seu computador (pip install -r requirements-ocr.txt) "
                "e cole o resultado em Importar → Disciplinas, ou envie um PDF com texto selecionável.") from e
        _LEITOR = easyocr.Reader(["pt", "en"], gpu=False, verbose=False)
    return _LEITOR


def agrupar_linhas(resultados):
    """Junta os blocos do OCR [(caixa, texto, conf)] em linhas, da esquerda para a direita."""
    itens = []
    for caixa, texto, *_ in resultados:
        ys = [p[1] for p in caixa]
        xs = [p[0] for p in caixa]
        itens.append({"t": texto.strip(), "x": min(xs), "yc": (min(ys) + max(ys)) / 2, "h": max(ys) - min(ys)})
    itens = [i for i in itens if i["t"]]
    itens.sort(key=lambda i: i["yc"])
    linhas, atual = [], []
    for i in itens:
        if atual and abs(i["yc"] - atual[-1]["yc"]) > 0.6 * max(i["h"], atual[-1]["h"], 1):
            linhas.append(atual)
            atual = []
        atual.append(i)
    if atual:
        linhas.append(atual)
    return ["  ".join(i["t"] for i in sorted(l, key=lambda i: i["x"])) for l in linhas]


def _ocr_bytes(dados):
    return "\n".join(agrupar_linhas(_leitor().readtext(dados, detail=1, paragraph=False)))


def extrair_texto(dados, nome_arquivo):
    """Devolve (texto, metodo). PDF com texto usa o texto do próprio PDF; senão, OCR."""
    nome = (nome_arquivo or "").lower()
    if nome.endswith(".pdf") or dados[:5] == b"%PDF-":
        texto = ""
        try:
            import io

            from pypdf import PdfReader
            texto = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(dados)).pages)
        except ImportError:
            pass
        if len(texto.strip()) >= 20:
            return texto, "texto do PDF"
        try:
            import io

            import pypdfium2 as pdfium
        except ImportError as e:
            raise OCRIndisponivel("Este PDF é uma imagem (sem texto) e falta o pypdfium2 para abri-lo. "
                                  "Envie como imagem (PNG/JPG) ou instale requirements-ocr.txt.") from e
        pdf = pdfium.PdfDocument(dados)
        partes = []
        for i in range(min(len(pdf), 10)):
            img = pdf[i].render(scale=2.5).to_pil()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            partes.append(_ocr_bytes(buf.getvalue()))
        return "\n".join(partes), "OCR (EasyOCR) das páginas do PDF"
    return _ocr_bytes(dados), "OCR (EasyOCR)"


DATA = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b")
DIAS = [("seg", 0), ("ter", 1), ("qua", 2), ("qui", 3), ("sex", 4), ("sab", 5)]
DIA_RE = re.compile(r"\b(seg(?:unda)?|ter(?:ca)?|qua(?:rta)?|qui(?:nta)?|sex(?:ta)?|sab(?:ado)?)\b[-\s]*(?:feira)?")
HORA = re.compile(r"\b\d{1,2}[:h]\d{2}\b")


def interpretar(texto, turma="", turno=""):
    """Transforma o texto da grade em linhas TSV aceitas por Importar → Disciplinas:
    Turma, Turno, D. Sem, Inicio, Fim, (restantes), Materia, Enc. — para o professor conferir antes."""
    saida = []
    for linha in (texto or "").splitlines():
        s = _sem_acento(linha)
        datas = list(DATA.finditer(s))
        if len(datas) < 2:
            continue
        def fmt(m):
            d, mes, ano = m.group(1), m.group(2), m.group(3)
            return "%s/%s%s" % (d, mes, "/" + ano if ano else "")
        ini, fim = fmt(datas[0]), fmt(datas[1])
        dias = []
        for m in DIA_RE.finditer(s):
            for pref, n in DIAS:
                if m.group(1).startswith(pref) and pref not in dias:
                    dias.append(pref)
        resto = linha
        for m in reversed(datas[:2]):
            resto = resto[:m.start()] + " " + resto[m.end():]
        resto = HORA.sub(" ", resto)
        resto = re.sub(r"(?i)\b(seg(unda)?|ter(ça|ca)?|qua(rta)?|qui(nta)?|sex(ta)?|s[aá]b(ado)?)\b[-\s]*(feira)?", " ", resto)
        enc = ""
        m_enc = re.search(r"\b(\d{1,3})\s*(?:enc\w*|aulas?)\b", resto, re.I)
        if m_enc:
            enc = m_enc.group(1)
            resto = resto[:m_enc.start()] + " " + resto[m_enc.end():]
        nome = re.sub(r"[^A-Za-zÀ-ÿ,.\-\s/]", " ", resto)
        nome = re.sub(r"\s+", " ", nome).strip(" -–.,/")
        nome = re.sub(r"(?i)^(?:(?:a|e|à|até|ate)\s+)+|(?:\s+(?:a|e|à|até|ate))+$", "", nome).strip(" -–.,/")
        if len(nome) < 4:
            continue
        saida.append("\t".join([turma, turno, ",".join(dias), ini, fim, "", nome, enc]))
    return "\n".join(saida)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("uso: python grade_ocr.py arquivo.(png|jpg|pdf) [TURMA] [Turno]")
    with open(sys.argv[1], "rb") as f:
        bruto = f.read()
    txt, metodo = extrair_texto(bruto, sys.argv[1])
    sys.stderr.write("# texto lido (%s):\n%s\n\n# linhas para importar:\n" % (metodo, txt))
    print(interpretar(txt, sys.argv[2] if len(sys.argv) > 2 else "", sys.argv[3] if len(sys.argv) > 3 else ""))
