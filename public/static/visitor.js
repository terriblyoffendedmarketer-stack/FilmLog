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

// ── Owner Auth Persistence across Mobile & Serverless ────────────────────────
(function() {
  function checkOwnerAuth() {
    try {
      var savedOwnerPwd = localStorage.getItem('fl_owner_pwd');
      if (savedOwnerPwd) {
        var pwdInput = document.querySelector('input[name="owner_password"]');
        if (pwdInput && !pwdInput.value) {
          pwdInput.value = savedOwnerPwd;
        }
        var btn = document.getElementById('generate-btn');
        if (btn && btn.textContent.indexOf('Owner Unlock') !== -1) {
          btn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg> Ask FilmLog Tonight';
        }
        var hint = document.querySelector('.inline-password-hint');
        if (hint) {
          hint.textContent = '🔓 Owner authenticated';
        }
      }
    } catch (e) {}
  }

  // Save owner password on input
  document.addEventListener('input', function(e) {
    if (e.target && e.target.name === 'owner_password' && e.target.value) {
      try {
        localStorage.setItem('fl_owner_pwd', e.target.value.trim());
      } catch (err) {}
    }
  });

  // Attach X-Owner-Password and X-Gemini-Key to ALL HTMX requests if available
  document.body.addEventListener('htmx:configRequest', function(evt) {
    try {
      var savedOwnerPwd = localStorage.getItem('fl_owner_pwd');
      if (savedOwnerPwd && evt.detail.headers) {
        evt.detail.headers['X-Owner-Password'] = savedOwnerPwd;
      }
      var guestKey = localStorage.getItem('fl_visitor_gemini_key') || localStorage.getItem('filmlog_gemini_key');
      if (guestKey && evt.detail.headers) {
        evt.detail.headers['X-Gemini-Key'] = guestKey;
      }
    } catch (e) {}
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() {
      checkOwnerAuth();
      if (window.updateApiKeyModalUI) window.updateApiKeyModalUI();
    });
  } else {
    checkOwnerAuth();
    if (window.updateApiKeyModalUI) window.updateApiKeyModalUI();
  }
})();

// ── Universal AI API Key Modal Management ──────────────────────────────────
window.openApiKeyModal = function() {
  var modal = document.getElementById('api-key-modal');
  if (!modal) return;
  modal.style.display = 'flex';
  document.body.style.overflow = 'hidden';
  window.updateApiKeyModalUI();
  var input = document.getElementById('modal-gemini-key-input');
  if (input) {
    var savedKey = localStorage.getItem('fl_visitor_gemini_key') || localStorage.getItem('filmlog_gemini_key') || '';
    input.value = savedKey;
    setTimeout(function() { input.focus(); }, 50);
  }
};

window.closeApiKeyModal = function() {
  var modal = document.getElementById('api-key-modal');
  if (!modal) return;
  modal.style.display = 'none';
  document.body.style.overflow = '';
  var feedback = document.getElementById('modal-key-test-feedback');
  if (feedback) feedback.style.display = 'none';
};

window.handleApiKeyBackdrop = function(e) {
  if (e.target && e.target.id === 'api-key-modal') {
    window.closeApiKeyModal();
  }
};

window.toggleModalKeyVisibility = function() {
  var inp = document.getElementById('modal-gemini-key-input');
  var btn = document.getElementById('modal-key-vis-btn');
  if (!inp || !btn) return;
  var isPw = inp.type === 'password';
  inp.type = isPw ? 'text' : 'password';
  btn.textContent = isPw ? 'Hide' : 'Show';
};

window.updateApiKeyModalUI = function() {
  var key = (localStorage.getItem('fl_visitor_gemini_key') || localStorage.getItem('filmlog_gemini_key') || '').trim();
  var banner = document.getElementById('modal-key-status-banner');
  var dot = document.getElementById('modal-key-status-dot');
  var text = document.getElementById('modal-key-status-text');
  var resetBtn = document.getElementById('modal-key-reset-btn');
  var navIndicator = document.getElementById('nav-key-indicator');
  var navBtn = document.querySelector('.nav-key-btn');

  if (key) {
    var masked = key.length > 8 ? key.slice(0, 6) + '...' + key.slice(-4) : '••••';
    if (dot) dot.className = 'api-key-status-dot active';
    if (text) text.textContent = 'Custom Key Active (' + masked + ')';
    if (resetBtn) resetBtn.style.display = 'inline-flex';
    if (navIndicator) navIndicator.classList.add('active');
    if (navBtn) navBtn.setAttribute('title', 'AI API Key: Custom active (' + masked + ')');
  } else {
    if (dot) dot.className = 'api-key-status-dot';
    if (text) text.textContent = 'Using Server Default Key';
    if (resetBtn) resetBtn.style.display = 'none';
    if (navIndicator) navIndicator.classList.remove('active');
    if (navBtn) navBtn.setAttribute('title', 'AI API Key: Server default (Click to update)');
  }
};

window.saveModalApiKey = function() {
  var input = document.getElementById('modal-gemini-key-input');
  var feedback = document.getElementById('modal-key-test-feedback');
  if (!input) return;
  var key = input.value.trim();
  if (!key) {
    window.clearModalApiKey();
    return;
  }
  try {
    localStorage.setItem('fl_visitor_gemini_key', key);
    localStorage.setItem('filmlog_gemini_key', key);
  } catch (e) {}

  window.updateApiKeyModalUI();

  if (feedback) {
    feedback.textContent = '✓ API Key saved and activated.';
    feedback.className = 'api-key-test-msg success';
    feedback.style.display = 'block';
  }

  // Also sync with any existing BYOK input on page
  var byokInp = document.getElementById('visitor-gemini-key-input');
  if (byokInp) byokInp.value = key;

  // Auto-close modal after brief pause
  setTimeout(function() {
    window.closeApiKeyModal();
  }, 750);
};

window.clearModalApiKey = function() {
  try {
    localStorage.removeItem('fl_visitor_gemini_key');
    localStorage.removeItem('filmlog_gemini_key');
  } catch (e) {}

  var input = document.getElementById('modal-gemini-key-input');
  if (input) input.value = '';

  window.updateApiKeyModalUI();

  var feedback = document.getElementById('modal-key-test-feedback');
  if (feedback) {
    feedback.textContent = 'Reset to server default key.';
    feedback.className = 'api-key-test-msg';
    feedback.style.display = 'block';
  }
};

window.testModalApiKey = async function() {
  var input = document.getElementById('modal-gemini-key-input');
  var feedback = document.getElementById('modal-key-test-feedback');
  var testBtn = document.getElementById('modal-key-test-btn');
  if (!input || !feedback) return;

  var key = input.value.trim() || localStorage.getItem('fl_visitor_gemini_key') || '';
  if (!key) {
    feedback.textContent = 'Please enter an API key to test.';
    feedback.className = 'api-key-test-msg error';
    feedback.style.display = 'block';
    return;
  }

  feedback.textContent = 'Testing key with Google Gemini…';
  feedback.className = 'api-key-test-msg';
  feedback.style.display = 'block';
  if (testBtn) testBtn.disabled = true;

  try {
    var resp = await fetch('/api/test-key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'gemini', key: key })
    });
    var data = await resp.json();
    if (data.ok) {
      feedback.textContent = data.message || '✓ Key is valid and working with Google Gemini!';
      feedback.className = 'api-key-test-msg success';
      try {
        localStorage.setItem('fl_visitor_gemini_key', key);
        localStorage.setItem('filmlog_gemini_key', key);
      } catch (e) {}
      window.updateApiKeyModalUI();
    } else {
      feedback.textContent = '✗ ' + (data.error || 'Test failed. Please check key.');
      feedback.className = 'api-key-test-msg error';
    }
  } catch (err) {
    feedback.textContent = '✗ Network error testing key: ' + err.message;
    feedback.className = 'api-key-test-msg error';
  } finally {
    if (testBtn) testBtn.disabled = false;
  }
};

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    var modal = document.getElementById('api-key-modal');
    if (modal && modal.style.display !== 'none') {
      window.closeApiKeyModal();
    }
  }
});

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
