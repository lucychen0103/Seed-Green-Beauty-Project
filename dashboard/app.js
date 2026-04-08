// ESG Funding Dashboard — main application script
// Reads from data.json (written by main.py, committed to gh-pages by CI).
// No API keys required client-side.

(function () {
  'use strict';

  const resultsEl = document.getElementById('results');
  const statsEl   = document.getElementById('stats');
  const searchEl  = document.getElementById('search');
  const filterBtns = document.querySelectorAll('.filter-btn');

  let allRecords = [];
  let activeTrack = 'all';
  let searchQuery = '';

  // ---------------------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------------------

  fetch('data.json')
    .then(function (res) {
      if (!res.ok) throw new Error('Failed to load data.json: ' + res.status);
      return res.json();
    })
    .then(function (data) {
      allRecords = data;
      render();
    })
    .catch(function (err) {
      resultsEl.innerHTML =
        '<p class="error">Could not load data. Run the pipeline to generate data.json.</p>';
      console.error(err);
    });

  // ---------------------------------------------------------------------------
  // Filtering & search
  // ---------------------------------------------------------------------------

  function filteredRecords() {
    return allRecords.filter(function (r) {
      var trackMatch =
        activeTrack === 'all' || r.source_track === activeTrack;

      var q = searchQuery.toLowerCase();
      var textMatch =
        !q ||
        (r.company_name || '').toLowerCase().includes(q) ||
        (r.notes || '').toLowerCase().includes(q) ||
        (Array.isArray(r.sustainability_keywords)
          ? r.sustainability_keywords.join(' ')
          : r.sustainability_keywords || ''
        ).toLowerCase().includes(q);

      return trackMatch && textMatch;
    });
  }

  // ---------------------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------------------

  function render() {
    var records = filteredRecords();
    statsEl.textContent = records.length + ' record' + (records.length !== 1 ? 's' : '');

    if (records.length === 0) {
      resultsEl.innerHTML = '<p class="empty">No records match your search.</p>';
      return;
    }

    resultsEl.innerHTML = records.map(function (r) {
      return card(r);
    }).join('');
  }

  function card(r) {
    var openBadge = '';
    if (r.is_open === true)       openBadge = '<span class="badge open">Open</span>';
    else if (r.is_open === false) openBadge = '<span class="badge closed">Closed</span>';

    var beautyBadge = r.beauty_alignment
      ? '<span class="badge beauty">Beauty Aligned</span>'
      : '';

    var keywords = Array.isArray(r.sustainability_keywords)
      ? r.sustainability_keywords
      : (r.sustainability_keywords || '').split(',').map(function (k) { return k.trim(); }).filter(Boolean);

    var keywordTags = keywords.map(function (k) {
      return '<span class="tag">' + esc(k) + '</span>';
    }).join('');

    var url = r.report_url
      ? '<a href="' + esc(r.report_url) + '" target="_blank" rel="noopener">View record</a>'
      : '';

    return [
      '<article class="card" role="listitem">',
      '  <div class="card-header">',
      '    <h2>' + esc(r.company_name || '—') + '</h2>',
      '    <div class="badges">' + openBadge + beautyBadge + '</div>',
      '  </div>',
      '  <dl class="card-meta">',
      '    <dt>Track</dt><dd>' + esc(r.source_track || '—') + '</dd>',
      '    <dt>Source</dt><dd>' + esc(r.source || '—') + '</dd>',
      '    <dt>Funding type</dt><dd>' + esc(r.funding_type || '—') + '</dd>',
      r.sector ? '    <dt>Sector</dt><dd>' + esc(r.sector) + '</dd>' : '',
      r.score_or_rating ? '    <dt>Score / rating</dt><dd>' + esc(r.score_or_rating) + '</dd>' : '',
      r.year_of_disclosure ? '    <dt>Year</dt><dd>' + esc(String(r.year_of_disclosure)) + '</dd>' : '',
      '  </dl>',
      r.notes ? '  <p class="card-notes">' + esc(r.notes) + '</p>' : '',
      keywordTags ? '  <div class="card-tags">' + keywordTags + '</div>' : '',
      url ? '  <div class="card-link">' + url + '</div>' : '',
      '  <p class="card-scraped">Scraped: ' + esc(r.scraped_at || '') + '</p>',
      '</article>',
    ].join('\n');
  }

  function esc(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ---------------------------------------------------------------------------
  // Events
  // ---------------------------------------------------------------------------

  filterBtns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      filterBtns.forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      activeTrack = btn.dataset.track;
      render();
    });
  });

  searchEl.addEventListener('input', function () {
    searchQuery = searchEl.value;
    render();
  });

  // ---------------------------------------------------------------------------
  // Export: CSV
  // ---------------------------------------------------------------------------

  document.getElementById('export-csv').addEventListener('click', function () {
    var records = filteredRecords();
    if (!records.length) return;

    var headers = [
      'company_name', 'source', 'source_track', 'funding_type',
      'sector', 'score_or_rating', 'year_of_disclosure', 'report_url',
      'beauty_alignment', 'is_open', 'sustainability_keywords',
      'scraped_at', 'notes',
    ];

    var rows = [headers].concat(records.map(function (r) {
      return headers.map(function (h) {
        var v = r[h];
        if (Array.isArray(v)) v = v.join('; ');
        if (v === null || v === undefined) v = '';
        v = String(v).replace(/"/g, '""');
        return '"' + v + '"';
      });
    }));

    var csv = rows.map(function (r) { return r.join(','); }).join('\n');
    var blob = new Blob([csv], { type: 'text/csv' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'esg-funding-' + _dateStamp() + '.csv';
    a.click();
    URL.revokeObjectURL(a.href);
  });

  // ---------------------------------------------------------------------------
  // Export: PDF (browser print)
  // ---------------------------------------------------------------------------

  document.getElementById('export-pdf').addEventListener('click', function () {
    window.print();
  });

  function _dateStamp() {
    return new Date().toISOString().slice(0, 10);
  }

}());
