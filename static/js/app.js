(function () {
  // Mobile menu toggle
  var burger = document.getElementById('burger');
  var menu = document.getElementById('menu');
  if (burger && menu) {
    burger.addEventListener('click', function () {
      var open = menu.classList.toggle('open');
      burger.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }

  // Confirm dialogs on destructive forms
  document.querySelectorAll('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) {
      if (!window.confirm(f.getAttribute('data-confirm'))) e.preventDefault();
    });
  });

  // Poster preview before upload
  var input = document.getElementById('poster-input');
  var preview = document.getElementById('poster-preview');
  if (input && preview) {
    input.addEventListener('change', function () {
      var file = input.files && input.files[0];
      if (!file) return;
      if (file.size > 5 * 1024 * 1024) {
        alert('That image is larger than 5 MB. Please choose a smaller one.');
        input.value = '';
        return;
      }
      preview.src = URL.createObjectURL(file);
      preview.classList.remove('hidden');
    });
  }

  // Countdown on event page
  var cd = document.getElementById('countdown');
  if (cd) {
    var target = new Date(cd.getAttribute('data-when')).getTime();
    var tick = function () {
      var diff = target - Date.now();
      if (diff <= 0) { cd.textContent = 'Happening now'; return; }
      var d = Math.floor(diff / 864e5), h = Math.floor(diff % 864e5 / 36e5), m = Math.floor(diff % 36e5 / 6e4);
      cd.textContent = 'Starts in ' + (d ? d + 'd ' : '') + h + 'h ' + m + 'm';
    };
    tick();
    setInterval(tick, 30000);
  }

  // Auto-dismiss success flashes
  document.querySelectorAll('.flash.success').forEach(function (el) {
    setTimeout(function () { el.style.opacity = '0'; setTimeout(function () { el.remove(); }, 400); }, 5000);
  });
})();
