// 公開中のページが古くなっていないかを、閲覧者の時計で確かめて帯を出す。
// 静的なページは「いつ見られているか」を知らないので、更新が止まったことは JS でしか言えない。
// ★三状態: 新しい（何も出さない）/ 古い（「更新が止まっています」）/ 確かめられない（そう言う。黙らない）。
// ★HTML を文字列で差し込まない（textContent だけ。Trusted Types で止まる）。通信するのは同じサイトの status.json だけ。
// 欄ごとの帯: [data-section-stale] の要素に data-fetched-at（その欄の取得時刻）と data-max-age-minutes を書くと、
//   閲覧者の時計でその分を超えたときに、その要素に帯を出す（通信しない）。
(function () {
  "use strict";
  function jst(ms) {
    return new Date(ms).toLocaleString("ja-JP", { timeZone: "Asia/Tokyo" });
  }
  function show(el, text) {
    el.textContent = text;
    el.hidden = false;
  }
  var sections = document.querySelectorAll("[data-section-stale]");
  for (var i = 0; i < sections.length; i++) {
    var el = sections[i];
    var fetched = Date.parse(el.getAttribute("data-fetched-at") || "");
    var limitMin = Number(el.getAttribute("data-max-age-minutes"));
    if (!isFinite(fetched) || !isFinite(limitMin) || limitMin <= 0) {
      show(el, "この欄の更新の状況を確かめられませんでした。最新の情報は、上の公式の情報で確かめてください。");
    } else if (Date.now() - fetched > limitMin * 60 * 1000) {
      show(el, "この欄の更新が止まっています（最後の取得: " + jst(fetched) + "）。最新の情報は、上の公式の情報で確かめてください。");
    }
  }
  var banner = document.querySelector("[data-stale-banner]");
  if (!banner) {
    return;
  }
  fetch("/status.json", { cache: "no-store", credentials: "omit" })
    .then(function (res) {
      if (!res.ok) {
        throw new Error("HTTP " + res.status);
      }
      return res.json();
    })
    .then(function (status) {
      var generated = Date.parse(status.generated_at);
      var limitMs = Number(status.max_status_age_minutes) * 60 * 1000;
      if (!isFinite(generated) || !isFinite(limitMs) || limitMs <= 0) {
        throw new Error("status.json の形が変です");
      }
      if (Date.now() - generated > limitMs) {
        show(banner, "更新が止まっています（最後の更新: " + jst(generated) + "）。最新の情報は、上の公式の情報で確かめてください。");
      }
    })
    .catch(function () {
      show(banner, "更新の状況を確かめられませんでした。最新の情報は、上の公式の情報で確かめてください。");
    });
})();
