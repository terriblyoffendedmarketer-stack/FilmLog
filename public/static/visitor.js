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

  // Listen for HTMX requests to intercept form data and save to localStorage
  document.body.addEventListener('htmx:configRequest', function (evt) {
    var path = evt.detail.path || '';
    var params = evt.detail.parameters || {};

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
    // Detect film detail page by looking for notes-section
    var notesSection = document.getElementById('notes-section');
    if (!notesSection) return;

    // Extract tmdb_id from the page URL
    var urlMatch = window.location.pathname.match(/\/films\/(\d+)/);
    if (!urlMatch) return;
    var tmdbId = urlMatch[1];

    // Overlay notes
    var savedNotes = storeGet(tmdbId, 'notes');
    if (savedNotes !== null) {
      var notesDisplay = notesSection.querySelector('.notes-text');
      if (notesDisplay) {
        notesDisplay.textContent = savedNotes || '(no notes yet)';
      }
    }

    // Overlay rating
    var savedRating = storeGet(tmdbId, 'rating');
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

  // Also run after HTMX swaps (for partials that get replaced)
  document.body.addEventListener('htmx:afterSwap', function () {
    setTimeout(overlayFilmDetail, 50);
  });

})();
