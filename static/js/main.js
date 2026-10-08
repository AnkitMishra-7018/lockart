(function () {
  var bar = document.getElementById("progress"), head = document.querySelector("header.site"), top = document.getElementById("top");
  function onScroll() {
    var h = document.documentElement, y = window.scrollY, max = h.scrollHeight - h.clientHeight;
    if (bar) bar.style.width = (max > 0 ? (y / max) * 100 : 0) + "%";
    if (head) head.classList.toggle("scrolled", y > 8);
    if (top) top.classList.toggle("show", y > 500);
  }
  window.addEventListener("scroll", onScroll, { passive: true }); onScroll();
  if (top) top.addEventListener("click", function () { window.scrollTo({ top: 0, behavior: "smooth" }); });
  var items = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
    }, { threshold: 0.12 });
    items.forEach(function (el) { io.observe(el); });
  } else { items.forEach(function (el) { el.classList.add("in"); }); }
  var msgs = document.querySelectorAll(".topbar span"), i = 0;
  if (msgs.length) setInterval(function () { msgs[i].classList.remove("on"); i = (i + 1) % msgs.length; msgs[i].classList.add("on"); }, 3500);
  var mb = document.querySelector(".menu-btn"), nl = document.querySelector("nav.links");
  if (mb && nl) mb.addEventListener("click", function () { nl.classList.toggle("open"); });
})();
(function () {
  var h = document.getElementById("hero"); if (!h) return;
  var s = h.querySelectorAll(".slide"), d = h.querySelector(".dots"), i = 0, t;
  var still = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  s.forEach(function (_, n) { var b = document.createElement("button"); b.type = "button"; b.setAttribute("aria-label", "Slide " + (n + 1)); b.onclick = function () { go(n); }; d.appendChild(b); });
  function reset() { clearInterval(t); if (!still) t = setInterval(function () { go(i + 1); }, 5000); }
  function go(n) { s[i].classList.remove("on"); d.children[i].classList.remove("on"); i = (n + s.length) % s.length; s[i].classList.add("on"); d.children[i].classList.add("on"); reset(); }
  h.querySelector(".prev").onclick = function () { go(i - 1); };
  h.querySelector(".next").onclick = function () { go(i + 1); };
  h.addEventListener("mouseenter", function () { clearInterval(t); });
  h.addEventListener("mouseleave", reset);
  go(0);
})();
(function () {
  var b = document.getElementById("fbtn"), f = document.getElementById("filters");
  if (b && f) { b.addEventListener("click", function () { f.classList.toggle("open"); });
    document.addEventListener("click", function (e) { if (f.classList.contains("open") && !f.contains(e.target) && e.target !== b) f.classList.remove("open"); }); }
})();
(function () {
  var q = document.getElementById("qty"); if (!q) return;
  document.querySelectorAll(".qty-btn").forEach(function (b) {
    b.addEventListener("click", function () {
      var v = (parseInt(q.value, 10) || 1) + parseInt(b.dataset.d, 10);
      q.value = Math.max(parseInt(q.min, 10) || 1, Math.min(parseInt(q.max, 10) || 99, v));
    });
  });
})();
(function () {
  var c = document.getElementById("confetti"); if (!c) return;
  var colors = ["#0F6E56", "#EF9F27", "#FAC775", "#5b21b6", "#B83B3B", "#2F8F9D"];
  for (var n = 0; n < 60; n++) {
    var p = document.createElement("i");
    p.style.left = Math.random() * 100 + "%"; p.style.background = colors[n % colors.length];
    p.style.animationDelay = Math.random() * 1.2 + "s"; p.style.animationDuration = 2.4 + Math.random() * 2 + "s";
    c.appendChild(p);
  }
  setTimeout(function () { c.remove(); }, 6000);
})();
(function () {
  var f = document.getElementById("imgfile"), p = document.getElementById("preview");
  if (!f || !p) return;
  f.addEventListener("change", function () {
    if (!f.files || !f.files[0]) return;
    var r = new FileReader(); r.onload = function (e) { p.src = e.target.result; p.classList.remove("hidden"); };
    r.readAsDataURL(f.files[0]);
  });
})();
(function () {
  document.querySelectorAll(".tsearch").forEach(function (box) {
    var table = document.getElementById(box.dataset.table); if (!table) return;
    var counter = box.parentNode.querySelector(".tcount");
    var rows = [].filter.call(table.querySelectorAll("tr"), function (tr) {
      return !tr.querySelector("th") && !tr.querySelector("td[colspan]");
    });
    function update() {
      var q = box.value.trim().toLowerCase(), shown = 0;
      rows.forEach(function (tr) {
        var hit = !q || tr.textContent.toLowerCase().indexOf(q) !== -1;
        tr.style.display = hit ? "" : "none"; if (hit) shown++;
      });
      if (counter) counter.textContent = shown + " of " + rows.length + " shown";
    }
    box.addEventListener("input", update); update();
  });
})();