(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };

  function lsGet(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* modo privado */ } }

  var perfilSalvo = null;
  try { perfilSalvo = JSON.parse(lsGet('presenca_perfil') || 'null'); } catch (e) { perfilSalvo = null; }

  function mostrar(id) {
    ['v-load', 'v-form', 'v-res'].forEach(function (x) { $(x).hidden = (x !== id); });
  }

  // Pede a localização já ao abrir a página (junto com o login/cadastro).
  var geoPromise = null;
  function geo(forcar) {
    if (geoPromise && !forcar) { return geoPromise; }
    geoPromise = new Promise(function (resolve) {
      if (!navigator.geolocation) { resolve(null); return; }
      navigator.geolocation.getCurrentPosition(
        function (p) { resolve({ lat: p.coords.latitude, lng: p.coords.longitude, acc: p.coords.accuracy }); },
        function () { resolve(null); },
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
    });
    return geoPromise;
  }

  function api(url, corpo) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Device-Key': lsGet('presenca_key') || '' },
      body: JSON.stringify(corpo || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, j: j }; });
    });
  }

  function resultado(classe, icone, titulo, nome, texto, tentar) {
    var box = $('v-res');
    box.className = 'card res ' + classe;
    $('res-icone').textContent = icone;
    $('res-titulo').textContent = titulo;
    $('res-nome').textContent = nome || '';
    $('res-texto').textContent = texto || '';
    $('res-tentar').hidden = !tentar;
    mostrar('v-res');
  }

  function checkin(renovar) {
    mostrar('v-load');
    geo(!!renovar).then(function (g) { return api('/api/checkin', { geo: g }); }).then(function (r) {
      var j = r.j;
      if (r.status === 401 || j.precisa_cadastro) { formulario(); return; }
      if (!j.ok) {
        resultado('info', 'ℹ️', 'Aula não encontrada', j.perfil ? j.perfil.nome : '', j.msg || 'Tente novamente em instantes.', true);
        return;
      }
      var quando = j.nova ? 'Chegada registrada às ' + j.chegada + '.' : 'Você já tinha registrado às ' + j.chegada + '.';
      if (j.status === 'ok') {
        resultado('ok', '✅', 'Presença registrada', j.perfil.nome, quando + ' Você chegou no horário.', false);
      } else if (j.status === 'atrasado') {
        resultado('warn', '⏰', 'Você está atrasado (' + j.atraso_txt + ')', j.perfil.nome,
          quando + ' Vá ao pedagógico pedir a autorização de entrada e entregue ao professor. ' +
          'Sem a autorização você não poderá assinar a lista.', false);
      } else {
        resultado('bad', '⛔', 'Atraso acima de ' + j.corte_txt, j.perfil.nome,
          quando + ' Com mais de ' + j.corte_txt + ' de atraso não é possível assinar a lista desta aula.', false);
      }
    }).catch(function () {
      resultado('info', '📶', 'Sem conexão', '', 'Não foi possível falar com o servidor. Verifique a internet e tente novamente.', true);
    });
  }

  function formulario(msg) {
    if (perfilSalvo) {
      $('nome').value = perfilSalvo.nome || '';
      $('matricula').value = perfilSalvo.matricula || '';
      $('turma').value = perfilSalvo.turma || '';
    }
    var e = $('f-erro');
    e.hidden = !msg;
    e.textContent = msg || '';
    mostrar('v-form');
  }

  $('f-cad').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var btn = ev.target.querySelector('button');
    btn.disabled = true;
    api('/api/registrar', {
      nome: $('nome').value, turma: $('turma').value, matricula: $('matricula').value
    }).then(function (r) {
      btn.disabled = false;
      if (!r.j.ok) { formulario(r.j.msg || 'Não foi possível cadastrar.'); return; }
      lsSet('presenca_key', r.j.key);
      perfilSalvo = r.j.aluno;
      lsSet('presenca_perfil', JSON.stringify(r.j.aluno));
      checkin();
    }).catch(function () { btn.disabled = false; formulario('Sem conexão. Tente novamente.'); });
  });

  $('res-tentar').addEventListener('click', function () { checkin(true); });
  $('matricula').addEventListener('input', function (e) { e.target.value = e.target.value.toUpperCase(); });

  geo();
  checkin();
})();
