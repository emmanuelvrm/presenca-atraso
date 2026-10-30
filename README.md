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

## Cadastro obrigatório antes da aula

Só abre a aula quem já tem: **turma → alunos → disciplina (início/fim) → aula com data e assunto**.
- `Turmas`: escolha a turma na lista (vinda da planilha `catalogo.json`), digite outra, ou adicione várias (uma por linha).
  A lista de alunos aceita `INF250005-Nome`, `INF240118Nome` e `INF240136 Nome`; o excedente (traços, números, símbolos) é limpo.
- `Disciplinas`: início, fim, dias da semana e cronograma (`Aula 1: assunto`, uma por linha).
- `Importar`: planilha de alocações, alunos, turmas e **grade em imagem/PDF** (PDF com texto lê direto; imagem usa EasyOCR).
- Menu lateral retrátil; na tela da aula, os cards (na turma / no horário / atrasados / ainda não entraram) filtram a lista.

### OCR (EasyOCR) opcional
É pesado (PyTorch). Para ativar no servidor: `docker build --build-arg OCR=1 .` (precisa de ~2 GB de disco e 1–2 GB de RAM; não cabe no plano gratuito do Render).
No seu computador: `pip install -r requirements-ocr.txt && python grade_ocr.py grade.png INF12T` e cole o resultado em Importar → Disciplinas.

### Banco persistente
Defina `DATABASE_URL` (Postgres) e os dados deixam de se perder nos reinícios. O catálogo (`catalogo.json`) é lido do arquivo e pode ser atualizado a partir da planilha.

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

## Calendário, dias da semana e data fim automática
Nos formulários de disciplina a data é escolhida no calendário, os dias da semana são botões (flags) e a data de fim é calculada
pelos encontros (pulando feriados). Se mexer na data de fim manualmente, ela deixa de ser recalculada até alterar início, encontros ou dias.

## Checagens de presença (menu Unidade)
- **Local (GPS):** leitura dentro do raio da escola = *válida*; fora ou sem GPS = *inválida*, e o professor escolhe **Validar** ou **Recusar** em "Atenção".
  A posição padrão é aproximada (Av. Padre Pedro Pinto, 1500); em **Unidade → Usar minha localização atual** o professor fixa a posição exata, dentro da escola.
- **Wi-Fi Grau Alunos:** o navegador não lê o nome do Wi-Fi, então o app grava o **IP público** de cada leitura e compara com os IPs registrados
  em Unidade (ou na variável `ESCOLA_IPS`). Coincidindo, aparece 📶. Dados móveis não recebem o selo.
- Aluno atrasado vê o aviso **PASSE NO PEDÁGIO** em destaque.
