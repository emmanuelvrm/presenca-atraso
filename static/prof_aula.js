(function () {
  'use strict';
  var app = document.getElementById('app');
  var AULA = app.getAttribute('data-aula');
  var base = '/prof/aula/' + AULA;
  var $ = function (id) { return document.getElementById(id); };
  var primeiro = true;
  var painelAberto = false;
  var ultimo = null;
  var assuntoSujo = false;

  function el(tag, cls, texto) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (texto !== undefined && texto !== null) n.textContent = texto;
    return n;
  }
  function limpar(n) { while (n.firstChild) n.removeChild(n.firstChild); }

  function chamar(url, corpo) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
      body: JSON.stringify(corpo || {})
    }).then(function (r) {
      if (r.status === 401) { window.location.href = '/prof/login?next=' + encodeURIComponent(window.location.pathname); }
      return r.json();
    });
  }

  function quem(item, extra) {
    var w = el('span', 'who');
    w.appendChild(el('b', null, item.nome));
    w.appendChild(el('small', null, item.matricula + (extra ? ' · ' + extra : '')));
    return w;
  }
  function badge(cls, texto) { return el('span', 'badge ' + cls, texto); }
  function botao(texto, cls, fn, desativado) {
    var b = el('button', 'btn sm ' + (cls || ''), texto);
    b.type = 'button';
    b.disabled = !!desativado;
    b.addEventListener('click', function () { b.disabled = true; fn().then(atualizar); });
    return b;
  }
  function vazio(ul, texto) { var li = el('li', 'vazio', texto); ul.appendChild(li); }

  function desenhar(e) {
    ultimo = e;
    var a = e.aula, t = e.totais;
    $('titulo').textContent = a.turma + (a.disciplina ? ' · ' + a.disciplina : '');
    $('sub').textContent = 'Início ' + a.inicio + ' · tolerância ' + a.tolerancia_min + ' min · corte ' +
      a.corte_txt + ' (até ' + a.limite + ') · ' + (a.status === 'aberta' ? 'aula aberta' : 'aula encerrada');
    $('relogio').textContent = 'agora ' + a.agora + ' · ' + a.decorridos_min + ' min de aula';
    $('t-turma').textContent = t.turma;
    $('t-ok').textContent = t.no_horario;
    $('t-atr').textContent = t.atrasados;
    $('t-nao').textContent = t.nao_leram;

    // assunto e andamento da disciplina
    $('curso').textContent = a.curso ? (a.curso.posicao + ' · ' + a.curso.texto) : '';
    if (document.activeElement !== $('assunto') && !assuntoSujo) $('assunto').value = a.assunto || '';

    // já entraram (no horário)
    var ul = $('l-ok'); limpar(ul);
    if (!e.entraram.length) vazio(ul, 'Ninguém entrou no horário ainda.');
    e.entraram.forEach(function (x) {
      var li = el('li');
      li.appendChild(quem(x, 'chegou ' + x.chegada + (x.manual ? ' · registro manual' : '')));
      var d = el('span', 'btns');
      if (x.local === true) d.appendChild(badge('ok', '📍 local'));
      else if (x.local === false) d.appendChild(badge('bad', '📍 sem local'));
      li.appendChild(d);
      ul.appendChild(li);
    });

    // atrasados
    ul = $('l-atr'); limpar(ul);
    if (!e.atrasados.length) vazio(ul, 'Nenhum atrasado até agora.');
    e.atrasados.forEach(function (x) {
      var li = el('li');
      li.appendChild(quem(x, 'chegou ' + x.chegada + ' (+' + x.atraso_txt + ')' + (x.manual ? ' · registro manual' : '')));
      var d = el('span', 'btns');
      if (x.local === true) d.appendChild(badge('ok', '📍 local'));
      else if (x.local === false) d.appendChild(badge('bad', '📍 sem local'));
      if (x.status === 'bloqueado') {
        d.appendChild(badge('bad', 'acima de ' + a.corte_txt + ' · não assina'));
      } else {
        if (x.autorizado === 1) d.appendChild(badge('ok', 'autorização OK'));
        else if (x.autorizado === 0) d.appendChild(badge('bad', 'sem autorização'));
        else d.appendChild(badge('warn', 'conferir'));
        d.appendChild(botao('Tem autorização', 'ok', function () { return chamar(base + '/autorizacao', { aluno_id: x.id, valor: 1 }); }, x.autorizado === 1));
        d.appendChild(botao('Sem autorização', 'bad', function () { return chamar(base + '/autorizacao', { aluno_id: x.id, valor: 0 }); }, x.autorizado === 0));
      }
      li.appendChild(d);
      ul.appendChild(li);
    });

    // não leram
    ul = $('l-nl'); limpar(ul);
    if (!e.nao_leram.length) vazio(ul, 'Todos já leram o QR code.');
    e.nao_leram.forEach(function (x) {
      var li = el('li');
      li.appendChild(quem(x));
      var d = el('span', 'btns');
      d.appendChild(botao('Registrar chegada manual', 'sec', function () { return chamar(base + '/manual', { aluno_id: x.id }); }));
      li.appendChild(d);
      ul.appendChild(li);
    });

    // atenção
    ul = $('l-atencao'); limpar(ul);
    $('s-atencao').hidden = !e.atencao.length;
    e.atencao.forEach(function (x) {
      var li = el('li');
      li.appendChild(quem(x, 'leu às ' + x.chegada));
      li.appendChild(badge('warn', x.flags.join(' · ')));
      ul.appendChild(li);
    });

    // não assinam
    $('n-nao').textContent = t.nao_assinam;
    $('aviso-conferir').textContent = t.a_conferir
      ? t.a_conferir + ' atrasado(s) com autorização ainda não conferida — marque na lista de atrasados.'
      : 'Todas as autorizações foram conferidas.';
    ul = $('l-nao'); limpar(ul);
    if (!e.nao_assinam.length) vazio(ul, 'Todos podem assinar.');
    e.nao_assinam.forEach(function (x) {
      var li = el('li');
      li.appendChild(quem(x, x.motivo));
      ul.appendChild(li);
    });
    $('painel-nao').hidden = !painelAberto;
    $('b-conferir').textContent = painelAberto ? 'Ocultar conferência' : 'Conferir antes de passar a lista';

    // saídas
    ul = $('l-saidas'); limpar(ul);
    if (!e.saidas.length) vazio(ul, 'Nenhuma saída registrada.');
    e.saidas.forEach(function (x) {
      var li = el('li');
      li.appendChild(quem(x, 'saiu às ' + x.hora + (x.motivo ? ' · ' + x.motivo : '')));
      li.appendChild(botao('Remover', 'sec', function () { return chamar(base + '/saida/' + x.id + '/remover'); }));
      ul.appendChild(li);
    });
    if (primeiro) {
      var sel = $('sa-aluno'); limpar(sel);
      e.alunos.forEach(function (al) {
        var o = el('option', null, al.nome); o.value = al.id; sel.appendChild(o);
      });
      primeiro = false;
    }
  }

  function atualizar() {
    return fetch(base + '/estado', { credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' } })
      .then(function (r) {
        if (r.status === 401) { window.location.href = '/prof/login?next=' + encodeURIComponent(window.location.pathname); return null; }
        return r.json();
      })
      .then(function (e) { if (e) desenhar(e); })
      .catch(function () { $('relogio').textContent = 'sem conexão — tentando de novo…'; });
  }

  $('b-conferir').addEventListener('click', function () {
    painelAberto = !painelAberto;
    if (ultimo) desenhar(ultimo);
    if (painelAberto) $('painel-nao').scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  $('sa-btn').addEventListener('click', function () {
    var id = $('sa-aluno').value;
    if (!id) return;
    chamar(base + '/saida', { aluno_id: parseInt(id, 10), motivo: $('sa-motivo').value }).then(function () {
      $('sa-motivo').value = '';
      atualizar();
    });
  });

  $('assunto').addEventListener('input', function () { assuntoSujo = true; });
  $('b-assunto').addEventListener('click', function () {
    var b = $('b-assunto'); b.disabled = true;
    chamar(base + '/assunto', { assunto: $('assunto').value }).then(function () {
      assuntoSujo = false; b.disabled = false; b.textContent = 'Salvo!';
      setTimeout(function () { b.textContent = 'Salvar assunto'; }, 1500);
      atualizar();
    });
  });

  atualizar();
  setInterval(function () { if (!document.hidden) atualizar(); }, 8000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) atualizar(); });
})();
