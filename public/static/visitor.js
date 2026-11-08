/*
 * visitor.js — client utilities & localStorage persistence for non-owner visitors.
 */

// Universal cinema back navigation with safe internal-domain fallback
window.handleSmartBack = function() {
  if (window.history.length > 1 && document.referrer && document.referrer.indexOf(window.location.host) !== -1) {
    window.history.back();
  } else {
    window.location.href = '/tonight';
  }
};

// ── Ephemeral Bring-Your-Own-Key (BYOK) for Visitors ─────────────────────────
window.handleVisitorKeySave = function() {
  var input = document.getElementById('visitor-gemini-key-input');
  if (!input) return;
  var key = input.value.trim();
  if (!key) {
    alert('Please paste a valid Google Gemini API key.');
    return;
  }
  try {
    localStorage.setItem('fl_visitor_gemini_key', key);
  } catch (e) {}

  var form = document.getElementById('calibration-form');
  if (form && window.htmx) {
    var btn = form.querySelector('#generate-btn');
    if (btn) btn.click();
    else if (form.requestSubmit) form.requestSubmit();
  }
};

// ── Client-Side Guest Watchlist (localStorage) ──────────────────────────────
window.getGuestWatchlist = function() {
  try {
    return JSON.parse(localStorage.getItem('fl_guest_watchlist') || '[]');
  } catch (e) {
    return [];
  }
};

window.isGuestBookmarked = function(tmdbId) {
  var list = window.getGuestWatchlist();
  return list.some(function(item) { return String(item.tmdb_id) === String(tmdbId); });
};

window.toggleGuestBookmark = function(tmdbId, title, year, posterUrl) {
  var list = window.getGuestWatchlist();
  var strId = String(tmdbId);
  var idx = list.findIndex(function(item) { return String(item.tmdb_id) === strId; });
  if (idx !== -1) {
    list.splice(idx, 1);
  } else {
    list.unshift({ tmdb_id: tmdbId, title: title, year: year, poster_url: posterUrl });
  }
  try {
    localStorage.setItem('fl_guest_watchlist', JSON.stringify(list));
  } catch (e) {}
  window.updateGuestBookmarkUI(tmdbId);
};

window.handleGuestBookmarkClick = function(el) {
  if (!el) return;
  var tmdbId = el.getAttribute('data-tmdb-id');
  var title = el.getAttribute('data-title');
  var year = el.getAttribute('data-year');
  var poster = el.getAttribute('data-poster');
  window.toggleGuestBookmark(tmdbId, title, year, poster);
};

window.updateGuestBookmarkUI = function(activeTmdbId) {
  var list = window.getGuestWatchlist();
  if (activeTmdbId) {
    var btn = document.getElementById('guest-bm-' + activeTmdbId);
    if (btn) {
      if (window.isGuestBookmarked(activeTmdbId)) {
        btn.classList.add('bookmarked');
        btn.innerHTML = '★ Saved to My List';
      } else {
        btn.classList.remove('bookmarked');
        btn.innerHTML = '+ Save to My List';
      }
    }
  }

  // Update navbar guest count
  var countEl = document.getElementById('guest-wl-count');
  var navLink = document.getElementById('nav-guest-watchlist');
  var tabCount = document.getElementById('guest-tab-count');
  if (countEl) countEl.textContent = list.length;
  if (tabCount) tabCount.textContent = list.length;
  if (navLink) {
    if (list.length > 0) navLink.style.display = 'inline-flex';
    else navLink.style.display = 'none';
  }
};

window.showGuestWatchlistView = function() {
  var grid = document.querySelector('.film-grid') || document.getElementById('films-grid');
  var tabBtn = document.getElementById('btn-filter-guest-wl');
  if (!grid) return;

  // Active tab styling
  document.querySelectorAll('.filter-btn').forEach(function(b) { b.classList.remove('active'); });
  if (tabBtn) tabBtn.classList.add('active');

  var list = window.getGuestWatchlist();
  if (!list || list.length === 0) {
    grid.innerHTML = '<div class="guest-empty-wl" style="grid-column: 1 / -1; padding: 48px 16px; text-align: center; color: var(--text-muted);">' +
                     '<h3 style="color: var(--text); font-size: 1.1rem; margin-bottom: 8px;">No films saved yet</h3>' +
                     '<p style="font-size: 0.88rem; max-width: 400px; margin: 0 auto 16px;">Browse the vault and click <strong>&quot;+ Save to My List&quot;</strong> on any film to bookmark it to your phone.</p>' +
                     '<a href="/films" class="btn-ghost" style="display: inline-block;">Browse All Films</a>' +
                     '</div>';
    return;
  }

  var html = '';
  list.forEach(function(film) {
    var posterHtml = film.poster_url
      ? '<img src="' + film.poster_url + '" alt="' + (film.title || '') + '" loading="lazy">'
      : '<div class="film-card-placeholder">' + ((film.title || '?').substring(0, 2)) + '</div>';

    html += '<a href="/films/' + film.tmdb_id + '" class="film-card">' +
            '<div class="film-card-poster">' + posterHtml + '</div>' +
            '<div class="film-card-info">' +
            '<div class="film-card-title">' + (film.title || '') + '</div>' +
            '<div class="film-card-year">' + (film.year || '') + '</div>' +
            '</div></a>';
  });

  grid.innerHTML = html;
};

// ── Vault Badge Case Toggle ─────────────────────────────────────────────────
window.toggleVaultBadgeCase = function() {
  var tray = document.getElementById('vault-badge-tray');
  var text = document.getElementById('badge-toggle-text');
  var arrow = document.getElementById('badge-toggle-arrow');
  if (!tray) return;
  if (tray.style.display === 'none' || !tray.style.display) {
    tray.style.display = 'block';
    if (text) text.textContent = 'Hide Badges';
    if (arrow) arrow.textContent = '▴';
  } else {
    tray.style.display = 'none';
    if (text) text.textContent = 'Show Badges';
    if (arrow) arrow.textContent = '▾';
  }
};

(function () {
  'use strict';

  var isOwner = document.body.getAttribute('data-owner') === 'true';
  if (isOwner) return; // Owner mode — everything goes to the real DB

  var STORE_PREFIX = 'fl:';

  function storeGet(tmdbId, field) {
    try {
      return localStorage.getItem(STORE_PREFIX + tmdbId + ':' + field);
    } catch (e) { return null; }
  }

  function storeSet(tmdbId, field, value) {
    try {
      localStorage.setItem(STORE_PREFIX + tmdbId + ':' + field, value);
    } catch (e) { /* quota exceeded, silently fail */ }
  }

  // ── Save visitor data after HTMX swaps ──────────────────────────────────

  document.body.addEventListener('htmx:configRequest', function (evt) {
    var path = evt.detail.path || '';
    var params = evt.detail.parameters || {};

    // Attach BYOK key if present
    try {
      var guestKey = localStorage.getItem('fl_visitor_gemini_key');
      if (guestKey && evt.detail.headers) {
        evt.detail.headers['X-Gemini-Key'] = guestKey;
      }
    } catch (e) {}

    // Notes save: /films/<id>/notes or /enrich/<id>
    var notesMatch = path.match(/\/films\/(\d+)\/notes/) || path.match(/\/enrich\/(\d+)$/);
    if (notesMatch) {
      var tmdbId = notesMatch[1];
      var notes = params.personal_notes || '';
      storeSet(tmdbId, 'notes', notes);
    }

    // Rating: /films/<id>/rating
    var ratingMatch = path.match(/\/films\/(\d+)\/rating/);
    if (ratingMatch) {
      storeSet(ratingMatch[1], 'rating', params.rating || '');
    }

    // Status: /films/<id>/status
    var statusMatch = path.match(/\/films\/(\d+)\/status/);
    if (statusMatch) {
      if (params.status) storeSet(statusMatch[1], 'status', params.status);
      if (params.rewatch !== undefined) storeSet(statusMatch[1], 'rewatch', params.rewatch);
    }
  });

  // ── Overlay localStorage data on page load ────────────────────────────────

  function overlayFilmDetail() {
    var notesSection = document.getElementById('notes-section');
    var urlMatch = window.location.pathname.match(/\/films\/(\d+)/);
    var activeId = urlMatch ? urlMatch[1] : null;

    if (activeId) {
      window.updateGuestBookmarkUI(activeId);
    } else {
      window.updateGuestBookmarkUI();
    }

    if (!notesSection || !activeId) return;

    // Overlay notes
    var savedNotes = storeGet(activeId, 'notes');
    if (savedNotes !== null) {
      var notesDisplay = notesSection.querySelector('.notes-text');
      if (notesDisplay) {
        notesDisplay.textContent = savedNotes || '(no notes yet)';
      }
    }

    // Overlay rating
    var savedRating = storeGet(activeId, 'rating');
    if (savedRating !== null) {
      var stars = document.querySelectorAll('.rating-star');
      var ratingVal = parseInt(savedRating, 10);
      stars.forEach(function (star) {
        var val = parseInt(star.getAttribute('data-value') || star.value, 10);
        if (val <= ratingVal) {
          star.classList.add('active');
        } else {
          star.classList.remove('active');
        }
      });
    }
  }

  // Run on page load
  overlayFilmDetail();

  // If visitor arrives with /films?filter=guest_watchlist, show list
  if (window.location.search.indexOf('filter=guest_watchlist') !== -1) {
    setTimeout(window.showGuestWatchlistView, 100);
  }

  // Also run after HTMX swaps (for partials that get replaced)
  document.body.addEventListener('htmx:afterSwap', function () {
    setTimeout(overlayFilmDetail, 50);
  });

})();
