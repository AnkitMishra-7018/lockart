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