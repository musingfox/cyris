/* Cyris Triage — swipe UI */
(function () {
  "use strict";

  const SWIPE_THRESHOLD = 100;
  const API = "/api/articles";

  let articles = [];
  let currentIndex = 0;
  let currentState = "pending";
  let isReadOnly = false;
  let lastAction = null; // {action, url, timer}

  const deck = document.getElementById("deck");
  const emptyState = document.getElementById("empty-state");
  const counter = document.getElementById("counter");
  const toast = document.getElementById("toast");
  const undoToast = document.getElementById("undo-toast");
  const undoMessage = document.getElementById("undo-message");
  const undoButton = document.getElementById("undo-button");
  const swipeFooter = document.getElementById("swipe-footer");
  const historyFooter = document.getElementById("history-footer");
  const prevBtn = document.getElementById("prev-btn");
  const nextBtn = document.getElementById("next-btn");

  /* ---- Data ---- */

  async function fetchArticles() {
    try {
      const res = await fetch(`${API}?limit=50&state=${currentState}`);
      const data = await res.json();
      articles = data.articles || [];
      currentIndex = 0;
      updateCounter();
      if (articles.length === 0) {
        showEmpty();
      } else {
        renderCurrentCard();
      }
      await fetchStats();
    } catch (err) {
      showToast("Failed to load articles");
    }
  }

  async function fetchStats() {
    try {
      const res = await fetch("/api/stats");
      const data = await res.json();
      document.getElementById("count-pending").textContent = data.pending || 0;
      document.getElementById("count-accepted").textContent = data.accepted || 0;
      document.getElementById("count-rejected").textContent = data.rejected || 0;
      document.getElementById("count-all").textContent = data.total || 0;
    } catch (err) {
      console.error("Failed to fetch stats:", err);
    }
  }

  function switchTab(state) {
    currentState = state;
    isReadOnly = state !== "pending";

    // Update active tab
    document.querySelectorAll("#filter-tabs .tab").forEach(function (tab) {
      if (tab.dataset.state === state) {
        tab.classList.add("active");
      } else {
        tab.classList.remove("active");
      }
    });

    // Toggle footer visibility
    if (isReadOnly) {
      swipeFooter.classList.add("hidden");
      historyFooter.classList.remove("hidden");
    } else {
      swipeFooter.classList.remove("hidden");
      historyFooter.classList.add("hidden");
    }

    // Hide undo toast when switching tabs
    hideUndoToast();

    fetchArticles();
  }

  /* ---- Rendering (safe DOM construction, no innerHTML) ---- */

  function renderCurrentCard() {
    deck.replaceChildren();
    emptyState.classList.add("hidden");
    if (currentIndex >= articles.length) {
      showEmpty();
      return;
    }
    const a = articles[currentIndex];
    const card = createCard(a);
    deck.appendChild(card);

    if (isReadOnly) {
      card.classList.add("read-only");
      updateHistoryButtons();
    } else {
      attachSwipe(card, a);
    }
  }

  function updateHistoryButtons() {
    prevBtn.disabled = currentIndex === 0;
    nextBtn.disabled = currentIndex >= articles.length - 1;
  }

  function navigatePrev() {
    if (currentIndex > 0) {
      currentIndex--;
      updateCounter();
      renderCurrentCard();
    }
  }

  function navigateNext() {
    if (currentIndex < articles.length - 1) {
      currentIndex++;
      updateCounter();
      renderCurrentCard();
    }
  }

  function createCard(article) {
    const card = document.createElement("div");
    card.className = "card";

    const snippet = stripHtml(article.content || "").slice(0, 200);
    const readMin = estimateReadTime(article.content || "", article.language);
    const scoreText = article.score != null ? String(Math.round(article.score)) : "\u2014";

    // Image (first img from content, fallback to favicon)
    var imgSrc = article.image_url || article.favicon_url || "";
    if (imgSrc) {
      var imgDiv = document.createElement("div");
      imgDiv.className = article.image_url ? "card-image" : "card-favicon";
      var img = document.createElement("img");
      img.src = imgSrc;
      img.alt = "";
      img.loading = "lazy";
      img.onerror = function () {
        // On error, try favicon fallback or hide
        if (article.image_url && article.favicon_url) {
          img.src = article.favicon_url;
          imgDiv.className = "card-favicon";
        } else {
          imgDiv.classList.add("hidden");
        }
      };
      imgDiv.appendChild(img);
      card.appendChild(imgDiv);
    }

    // Meta row
    const meta = document.createElement("div");
    meta.className = "card-meta";

    const scoreBadge = document.createElement("span");
    scoreBadge.className = "badge badge-score";
    scoreBadge.textContent = scoreText;
    meta.appendChild(scoreBadge);

    if (article.language) {
      const langBadge = document.createElement("span");
      langBadge.className = "badge";
      langBadge.textContent = article.language;
      meta.appendChild(langBadge);
    }

    if (article.domain) {
      const domainBadge = document.createElement("span");
      domainBadge.className = "badge";
      domainBadge.textContent = article.domain;
      meta.appendChild(domainBadge);
    }

    card.appendChild(meta);

    // Source + author line
    const sourceDiv = document.createElement("div");
    sourceDiv.className = "card-source-line";
    var sourceText = article.source_name || "";
    if (article.author) {
      sourceText += " \u00b7 " + article.author;
    }
    sourceDiv.textContent = sourceText;
    card.appendChild(sourceDiv);

    // Title
    const titleDiv = document.createElement("div");
    titleDiv.className = "card-title";
    const titleLink = document.createElement("a");
    titleLink.href = article.url || "#";
    titleLink.target = "_blank";
    titleLink.rel = "noopener";
    titleLink.textContent = article.title || "";
    titleDiv.appendChild(titleLink);
    card.appendChild(titleDiv);

    // Snippet
    const snippetDiv = document.createElement("div");
    snippetDiv.className = "card-snippet";
    snippetDiv.textContent = snippet + (snippet.length >= 200 ? "..." : "");
    card.appendChild(snippetDiv);

    // Footer
    const footer = document.createElement("div");
    footer.className = "card-footer";
    const dateSpan = document.createElement("span");
    dateSpan.textContent = formatDateFull(article.published_at);
    const timeSpan = document.createElement("span");
    timeSpan.textContent = readMin + " min read";
    footer.appendChild(dateSpan);
    footer.appendChild(timeSpan);
    card.appendChild(footer);

    return card;
  }

  function stripHtml(html) {
    var tmp = document.createElement("div");
    tmp.textContent = html;
    // Use textContent to set, then read back — strips tags if any remain
    var text = html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
    return text;
  }

  function estimateReadTime(content, lang) {
    if (!content) return "?";
    if (lang === "zh") {
      return Math.max(1, Math.ceil(content.length / 1000));
    }
    const words = content.split(/\s+/).length;
    return Math.max(1, Math.ceil(words / 200));
  }

  function formatDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return m + "-" + day;
  }

  function formatDateFull(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    const h = String(d.getHours()).padStart(2, "0");
    const min = String(d.getMinutes()).padStart(2, "0");
    return y + "-" + m + "-" + day + " " + h + ":" + min;
  }

  function updateCounter() {
    if (isReadOnly) {
      if (articles.length > 0) {
        counter.textContent = (currentIndex + 1) + " / " + articles.length + " articles";
      } else {
        counter.textContent = "";
      }
    } else {
      const remaining = articles.length - currentIndex;
      counter.textContent = remaining > 0 ? remaining + " remaining" : "";
    }
  }

  function showEmpty() {
    deck.replaceChildren();
    emptyState.classList.remove("hidden");
  }

  function showToast(msg) {
    toast.textContent = msg;
    toast.classList.remove("hidden");
    setTimeout(function () {
      toast.classList.add("hidden");
    }, 2000);
  }

  function showUndoToast(action, url, reason) {
    hideUndoToast(); // Clear previous undo

    undoMessage.textContent = action === "accept" ? "Accepted" : "Rejected";

    lastAction = {
      action: action,
      url: url,
      timer: setTimeout(function () {
        hideUndoToast();
      }, 5000),
    };

    undoToast.classList.remove("hidden");
  }

  function hideUndoToast() {
    if (lastAction && lastAction.timer) {
      clearTimeout(lastAction.timer);
    }
    lastAction = null;
    undoToast.classList.add("hidden");
  }

  async function performUndo() {
    if (!lastAction) return;

    const url = lastAction.url;
    hideUndoToast();

    try {
      const res = await fetch(API + "/undo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url }),
      });
      const data = await res.json();
      if (!data.ok) {
        showToast("Undo failed: " + (data.error || "unknown"));
        return;
      }

      // Move back one card
      if (currentIndex > 0) {
        currentIndex--;
      }
      updateCounter();
      renderCurrentCard();
      await fetchStats();
    } catch (err) {
      showToast("Network error");
    }
  }

  /* ---- Swipe ---- */

  function attachSwipe(card, article) {
    let startX = 0;
    let currentX = 0;
    let dragging = false;

    function onStart(e) {
      dragging = true;
      startX = e.touches ? e.touches[0].clientX : e.clientX;
      currentX = 0;
      card.classList.add("dragging");
    }

    function onMove(e) {
      if (!dragging) return;
      e.preventDefault();
      const x = e.touches ? e.touches[0].clientX : e.clientX;
      currentX = x - startX;
      const rotate = currentX * 0.05;
      card.style.transform = "translateX(" + currentX + "px) rotate(" + rotate + "deg)";

      card.classList.toggle("accept-hint", currentX > SWIPE_THRESHOLD);
      card.classList.toggle("reject-hint", currentX < -SWIPE_THRESHOLD);
    }

    function onEnd() {
      if (!dragging) return;
      dragging = false;
      card.classList.remove("dragging");

      if (currentX > SWIPE_THRESHOLD) {
        card.classList.add("fly-right");
        postAction("accept", article.url);
      } else if (currentX < -SWIPE_THRESHOLD) {
        card.classList.add("fly-left");
        postAction("reject", article.url, "not_interested");
      } else {
        card.style.transform = "";
        card.classList.remove("accept-hint", "reject-hint");
      }
    }

    card.addEventListener("touchstart", onStart, { passive: true });
    card.addEventListener("touchmove", onMove, { passive: false });
    card.addEventListener("touchend", onEnd);
    card.addEventListener("mousedown", onStart);
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onEnd);
  }

  /* ---- Actions ---- */

  // Blocks a second verdict while one is in flight or its card is still on
  // screen (the re-render lags 300ms), so a double-click cannot reject the
  // next, unseen article.
  let actionInFlight = false;

  async function postAction(action, url, reason) {
    if (actionInFlight) return;
    actionInFlight = true;

    // Clear previous undo when swiping to next card
    hideUndoToast();

    try {
      const res = await fetch(API + "/" + action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url, reason: reason }),
      });
      const data = await res.json();
      if (!data.ok) {
        showToast("Error: " + (data.error || "unknown"));
        renderCurrentCard();
        actionInFlight = false;
        return;
      }
    } catch (err) {
      showToast("Network error");
      renderCurrentCard();
      actionInFlight = false;
      return;
    }

    showUndoToast(action, url, reason);
    currentIndex++;
    updateCounter();
    setTimeout(function () {
      renderCurrentCard();
      actionInFlight = false;
    }, 300);
    await fetchStats();
  }

  /* ---- Keyboard shortcuts ---- */

  document.addEventListener("keydown", function (e) {
    if (currentIndex >= articles.length) return;
    var article = articles[currentIndex];
    var card = deck.querySelector(".card");
    if (!card) return;

    if (isReadOnly) {
      // In read-only mode, arrow keys navigate
      if (e.key === "ArrowLeft") {
        navigatePrev();
      } else if (e.key === "ArrowRight") {
        navigateNext();
      }
    } else {
      // In pending mode, arrow keys trigger actions
      if (e.key === "ArrowRight" || e.key === "l") {
        card.classList.add("fly-right");
        postAction("accept", article.url);
      } else if (e.key === "ArrowLeft" || e.key === "h") {
        card.classList.add("fly-left");
        postAction("reject", article.url, "not_interested");
      }
    }
  });

  /* ---- Event Listeners ---- */

  // Filter tab clicks
  document.querySelectorAll("#filter-tabs .tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      switchTab(tab.dataset.state);
    });
  });

  // Undo button
  undoButton.addEventListener("click", performUndo);

  // History navigation buttons
  prevBtn.addEventListener("click", navigatePrev);
  nextBtn.addEventListener("click", navigateNext);

  /* ---- Init ---- */

  fetchArticles();
})();
