# Presença & Atrasos (MVP)

QR code único → o aluno se cadastra uma vez (nome, turma, matrícula) → o servidor gera uma chave do aparelho
e vincula a matrícula a ele → nas próximas aulas, escanear o QR já registra a chegada.

## Rodar

```bash
pip install -r requirements.txt
python app.py                      # http://localhost:8000  (senha do professor impressa na 1ª execução)
python test_app.py                 # teste do fluxo completo
```

Docker (na VM Oracle): `docker build -t presenca . && docker run -d -p 8000:8000 -v presenca-data:/data -e BASE_URL=https://SEU-DOMINIO -e PROF_SENHA=... presenca`

## Uso em aula

1. **Uma vez:** `/prof/turmas` → crie a turma colando "matrícula nome" da ata (há `exemplo_turma.txt`). `/prof/qr` → imprima o QR.
2. **Todo dia:** `/prof` → Abrir aula (turma, horário de início, tolerância 10 min, corte 90 min).
3. Aluno escaneia: no horário = verde; atrasado = orientado a pegar autorização no pedagógico; acima do corte = não assina.
4. Sua tela (`/prof/aula/N`, atualiza a cada 8 s) mostra **só** quem não leu o QR e quem está atrasado.
5. Antes de passar a lista: marque "Tem autorização / Sem autorização" nos atrasados e toque em **Conferir antes de passar a lista**:
   a caixa vermelha lista quem NÃO assina (não leu, atrasado sem autorização, acima do corte). Ninguém é riscado na ata.
6. **Encerrar aula** gera o relatório para o pedagógico (imprimir/PDF, CSV, texto ou e-mail).

## Antifraude

- Matrícula presa a um aparelho; um aparelho só serve a um aluno. Troca de celular: `Turmas → Liberar aparelho`.
- O QR fixo só vale com aula aberta para a turma do aluno. Limite conhecido: quem fotografa o QR e o manda a um colega
  que já tem cadastro no próprio aparelho continua sendo a própria presença dele. A fraude real é um aluno fora da escola escanear.
  Para reduzir: configure a localização (abaixo), que marca leituras fora da unidade na seção "Atenção".

## Variáveis de ambiente

| Variável | Para quê |
|---|---|
| `PROF_SENHA` | senha do professor (se vazia, é gerada em `data/.senha`) |
| `BASE_URL` | endereço público com HTTPS (vai no QR) |
| `PEDAGOGICO_EMAIL` | habilita o botão "Enviar por e-mail" no relatório |
| `TOLERANCIA_MIN`, `CORTE_MIN` | padrões (10 e 90) |
| `UNIDADE_LAT`, `UNIDADE_LNG`, `RAIO_M` | opcional: marca leituras fora do raio (padrão 250 m); exige HTTPS |
| `DATA_DIR` | onde fica o SQLite |

Guarda apenas matrícula, nome, horário (e coordenadas, se a localização estiver ativa). Avise os alunos (LGPD).
