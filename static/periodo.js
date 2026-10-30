(function () {
  'use strict';
  document.querySelectorAll('[data-periodo]').forEach(function (box) {
    var ini = box.querySelector('[name=ini]'), fim = box.querySelector('[name=fim]'),
        enc = box.querySelector('[name=enc]'), aviso = box.querySelector('[data-fim-aviso]'),
        dias = box.querySelectorAll('[name=dias]'), fer = {};
    JSON.parse(box.getAttribute('data-feriados') || '[]').forEach(function (d) { fer[d] = 1; });
    var manual = false;
    function iso(d) {
      return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2);
    }
    function calcular() {
      var sel = {};
      dias.forEach(function (c) { if (c.checked) sel[{ seg: 1, ter: 2, qua: 3, qui: 4, sex: 5, sab: 6 }[c.value]] = 1; });
      var n = parseInt(enc.value, 10);
      if (!ini.value || !n || !Object.keys(sel).length) { return; }
      var p = ini.value.split('-'), d = new Date(+p[0], +p[1] - 1, +p[2]), c = 0, guarda = 0;
      while (guarda++ < 900) {
        if (sel[d.getDay()] && !fer[iso(d)]) { c++; if (c === n) { break; } }
        d.setDate(d.getDate() + 1);
      }
      if (c === n) { fim.value = iso(d); aviso.textContent = 'calculada: ' + n + ' encontros (sem feriados)'; }
    }
    function auto() { if (!manual) { calcular(); } }
    [ini, enc].forEach(function (e) { e.addEventListener('input', auto); e.addEventListener('change', auto); });
    dias.forEach(function (c) { c.addEventListener('change', auto); });
    fim.addEventListener('input', function () { manual = true; aviso.textContent = 'ajustada por você'; });
    // ao abrir a página com o fim já salvo, não recalcula; só quando o professor mexe nos outros campos
    if (!fim.value) { auto(); }
    // abre o calendário ao clicar em qualquer ponto do campo de data
    [ini, fim].forEach(function (e) {
      e.addEventListener('click', function () { if (e.showPicker) { try { e.showPicker(); } catch (x) { /* ok */ } } });
    });
    // mexer em ini/enc/dias volta ao modo automático
    [ini, enc].forEach(function (e) { e.addEventListener('input', function () { manual = false; auto(); }); });
    dias.forEach(function (c) { c.addEventListener('change', function () { manual = false; auto(); }); });
  });
})();
