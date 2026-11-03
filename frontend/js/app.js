/* InventraMX single-page frontend. All data is loaded from the REST API via XHR (see api.js). */
(function () {
  'use strict';

  var VIEWS = ['dashboard', 'products', 'warehouses', 'locations', 'stock', 'movements'];
  var cache = { warehouses: [], locations: [], products: [] };

  // ---------- DOM helpers ----------

  function $(selector, root) { return (root || document).querySelector(selector); }
  function $all(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }

  /** Create an element; text content is always set via textContent to avoid HTML injection. */
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === 'text') node.textContent = attrs[key];
      else if (key === 'className') node.className = attrs[key];
      else if (key.indexOf('on') === 0) node.addEventListener(key.slice(2), attrs[key]);
      else node.setAttribute(key, attrs[key]);
    });
    (children || []).forEach(function (child) {
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  function td(value, className) {
    return el('td', { text: value === null || value === undefined ? '' : String(value), className: className || '' });
  }

  function renderRows(tbody, items, rowFn, emptyText) {
    tbody.replaceChildren();
    if (!items.length) {
      var colspan = tbody.closest('table').querySelectorAll('thead th').length;
      tbody.appendChild(el('tr', {}, [el('td', { colspan: colspan, className: 'text-center text-slate-400 py-6', text: emptyText || 'No records.' })]));
      return;
    }
    items.forEach(function (item) { tbody.appendChild(rowFn(item)); });
  }

  var toastTimer;
  function toast(message, isError) {
    var box = $('#toast');
    box.textContent = message;
    box.className = 'fixed top-4 right-4 z-50 px-4 py-2 rounded shadow text-sm text-white ' + (isError ? 'bg-red-600' : 'bg-emerald-600');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { box.classList.add('hidden'); }, isError ? 6000 : 3000);
  }

  function showError(err) { toast(err.message || String(err), true); }

  function formToJson(form) {
    var data = {};
    new FormData(form).forEach(function (value, key) {
      var input = form.elements[key];
      if (value === '') { data[key] = input && input.tagName === 'SELECT' ? null : value; return; }
      data[key] = input && input.type === 'number' ? Number(value) : value;
    });
    return data;
  }

  function formatDate(iso) { return iso ? new Date(iso).toLocaleString() : ''; }

  function debounce(fn, wait) {
    var timer;
    return function () {
      var args = arguments;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(null, args); }, wait);
    };
  }

  function results(page) { return page && page.results ? page.results : (page || []); }

  // ---------- Lookups (warehouses / locations / products dropdowns) ----------

  var LABELS = {
    warehouses: function (w) { return w.code + ' - ' + w.name; },
    locations: function (l) { return l.warehouse_code + '/' + l.code + ' (' + l.location_type + ')'; },
    products: function (p) { return p.sku + ' - ' + p.name; }
  };

  function refreshLookups() {
    return Promise.all([
      Api.get('/warehouses/', { limit: 1000 }),
      Api.get('/locations/', { limit: 1000, is_active: 'true' }),
      Api.get('/products/', { limit: 1000, is_active: 'true' })
    ]).then(function (pages) {
      cache.warehouses = results(pages[0]);
      cache.locations = results(pages[1]);
      cache.products = results(pages[2]);
      $all('select[data-options]').forEach(fillSelect);
    });
  }

  function fillSelect(select) {
    var kind = select.getAttribute('data-options');
    var previous = select.value;
    select.replaceChildren();
    var empty = select.getAttribute('data-empty');
    if (empty !== null) select.appendChild(el('option', { value: '', text: empty }));
    cache[kind].forEach(function (item) {
      select.appendChild(el('option', { value: item.id, text: LABELS[kind](item) }));
    });
    if (previous && $all('option', select).some(function (o) { return o.value === previous; })) select.value = previous;
  }

  // ---------- Views ----------

  function loadDashboard() {
    return Api.get('/reports/summary/').then(function (data) {
      var cards = [
        ['Warehouses', data.warehouses], ['Locations', data.locations],
        ['Products', data.products], ['Units on hand', data.total_units]
      ];
      $('#summary-cards').replaceChildren.apply($('#summary-cards'), cards.map(function (c) {
        return el('div', { className: 'card' }, [
          el('div', { className: 'text-xs uppercase text-slate-500', text: c[0] }),
          el('div', { className: 'text-2xl font-semibold mt-1', text: String(c[1]) })
        ]);
      }));
      var list = $('#summary-movements');
      list.replaceChildren();
      Object.keys(data.movements).forEach(function (type) {
        list.appendChild(el('li', { className: 'flex justify-between' }, [
          el('span', { className: 'badge badge-' + type, text: type }),
          el('span', { className: 'font-medium', text: String(data.movements[type]) })
        ]));
      });
      renderRows($('#low-stock-body'), data.low_stock, function (p) {
        return el('tr', {}, [td(p.sku), td(p.name), td(p.total_quantity, 'text-right text-red-600 font-medium'), td(p.reorder_level, 'text-right')]);
      }, 'All products are above their reorder level.');
    });
  }

  function toggleActive(path, item) {
    return function () {
      Api.patch(path + item.id + '/', { is_active: !item.is_active })
        .then(function () { toast('Updated.'); return refreshLookups(); })
        .then(loadCurrent)
        .catch(showError);
    };
  }

  function deleteRecord(path, item, label) {
    return function () {
      if (!window.confirm('Delete ' + label + '?')) return;
      Api.del(path + item.id + '/')
        .then(function () { toast('Deleted.'); return refreshLookups(); })
        .then(loadCurrent)
        .catch(showError);
    };
  }

  function actionCell(path, item, label) {
    return el('td', { className: 'text-right whitespace-nowrap space-x-2' }, [
      el('button', { type: 'button', className: 'btn-link', text: item.is_active ? 'Deactivate' : 'Activate', onclick: toggleActive(path, item) }),
      el('button', { type: 'button', className: 'btn-link', text: 'Delete', onclick: deleteRecord(path, item, label) })
    ]);
  }

  function loadProducts() {
    return Api.get('/products/', { search: $('#product-search').value }).then(function (page) {
      renderRows($('#products-body'), results(page), function (p) {
        var low = p.is_active && p.total_quantity <= p.reorder_level;
        return el('tr', {}, [
          td(p.sku, 'font-mono'), td(p.name), td(p.unit),
          td(p.total_quantity, 'text-right' + (low ? ' text-red-600 font-medium' : '')),
          td(p.reorder_level, 'text-right'), td(p.is_active ? 'Yes' : 'No'),
          actionCell('/products/', p, p.sku)
        ]);
      }, 'No products found.');
    });
  }

  function loadWarehouses() {
    return Api.get('/warehouses/').then(function (page) {
      renderRows($('#warehouses-body'), results(page), function (w) {
        return el('tr', {}, [td(w.code, 'font-mono'), td(w.name), td(w.address), td(w.is_active ? 'Yes' : 'No'), actionCell('/warehouses/', w, w.code)]);
      }, 'No warehouses yet.');
    });
  }

  function loadLocations() {
    return Api.get('/locations/', { warehouse: $('#location-filter').value }).then(function (page) {
      renderRows($('#locations-body'), results(page), function (l) {
        return el('tr', {}, [td(l.warehouse_code), td(l.code, 'font-mono'), td(l.location_type), td(l.is_active ? 'Yes' : 'No'), actionCell('/locations/', l, l.code)]);
      }, 'No locations yet.');
    });
  }

  function loadStock() {
    return Api.get('/stock/', { warehouse: $('#stock-warehouse').value, search: $('#stock-search').value }).then(function (page) {
      renderRows($('#stock-body'), results(page), function (s) {
        return el('tr', {}, [td(s.product_sku, 'font-mono'), td(s.product_name), td(s.warehouse_code), td(s.location_code), td(s.quantity, 'text-right font-medium'), td(formatDate(s.updated_at))]);
      }, 'No stock on hand.');
    });
  }

  function loadMovements() {
    return Api.get('/movements/', { movement_type: $('#movement-filter').value }).then(function (page) {
      renderRows($('#movements-body'), results(page), function (m) {
        return el('tr', {}, [
          td(formatDate(m.created_at)),
          el('td', {}, [el('span', { className: 'badge badge-' + m.movement_type, text: m.movement_type })]),
          td(m.product_sku, 'font-mono'), td(m.from_location_label), td(m.to_location_label),
          td(m.quantity, 'text-right'), td(m.reference)
        ]);
      }, 'No movements recorded.');
    });
  }

  var LOADERS = {
    dashboard: loadDashboard, products: loadProducts, warehouses: loadWarehouses,
    locations: loadLocations, stock: loadStock, movements: loadMovements
  };

  function currentView() {
    var name = window.location.hash.replace('#', '');
    return VIEWS.indexOf(name) === -1 ? 'dashboard' : name;
  }

  function loadCurrent() { return LOADERS[currentView()]().catch(showError); }

  function showView() {
    var name = currentView();
    $all('.view').forEach(function (v) { v.classList.toggle('hidden', v.id !== 'view-' + name); });
    $all('.nav-link').forEach(function (a) { a.classList.toggle('active', a.getAttribute('data-view') === name); });
    loadCurrent();
  }

  // ---------- Forms ----------

  function bindCreateForm(formId, path, successMessage) {
    var form = $(formId);
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      Api.post(path, formToJson(form))
        .then(function () {
          toast(successMessage);
          form.reset();
          return refreshLookups();
        })
        .then(loadCurrent)
        .then(updateMovementForm)
        .catch(showError);
    });
  }

  var MOVEMENT_HINTS = {
    inbound: 'Receive goods into the destination location.',
    outbound: 'Pick / ship goods out of the source location.',
    transfer: 'Move goods between two locations.',
    adjustment: 'Choose a destination to add stock (found) or a source to remove stock (lost/damaged) — not both.'
  };

  function updateMovementForm() {
    var type = $('#movement-type').value;
    var showFrom = type !== 'inbound';
    var showTo = type !== 'outbound';
    var from = $('label[data-for="from"]');
    var to = $('label[data-for="to"]');
    from.classList.toggle('hidden', !showFrom);
    to.classList.toggle('hidden', !showTo);
    if (!showFrom) from.querySelector('select').value = '';
    if (!showTo) to.querySelector('select').value = '';
    $('#movement-hint').textContent = MOVEMENT_HINTS[type];
  }

  function checkApi() {
    Api.get('/health/')
      .then(function () { $('#api-status').textContent = 'API: online'; })
      .catch(function () { $('#api-status').textContent = 'API: offline'; });
  }

  document.addEventListener('DOMContentLoaded', function () {
    bindCreateForm('#product-form', '/products/', 'Product created.');
    bindCreateForm('#warehouse-form', '/warehouses/', 'Warehouse created.');
    bindCreateForm('#location-form', '/locations/', 'Location created.');
    bindCreateForm('#movement-form', '/movements/', 'Movement recorded.');

    $('#movement-type').addEventListener('change', updateMovementForm);
    $('#product-search').addEventListener('input', debounce(loadCurrent, 300));
    $('#stock-search').addEventListener('input', debounce(loadCurrent, 300));
    $('#stock-warehouse').addEventListener('change', loadCurrent);
    $('#location-filter').addEventListener('change', loadCurrent);
    $('#movement-filter').addEventListener('change', loadCurrent);
    window.addEventListener('hashchange', showView);

    updateMovementForm();
    checkApi();
    refreshLookups().catch(showError).then(showView);
  });
})();
