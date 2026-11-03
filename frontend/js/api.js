/* Minimal XMLHttpRequest wrapper for the InventraMX REST API. */
(function (global) {
  'use strict';

  var API_BASE = global.INVENTRA_API_BASE || '/api';

  function buildUrl(path, params) {
    var url = API_BASE + path;
    if (params) {
      var query = Object.keys(params)
        .filter(function (k) { return params[k] !== undefined && params[k] !== null && params[k] !== ''; })
        .map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]); })
        .join('&');
      if (query) url += (url.indexOf('?') === -1 ? '?' : '&') + query;
    }
    return url;
  }

  function formatErrors(data, status) {
    if (!data) return 'Request failed (HTTP ' + status + ')';
    if (typeof data === 'string') return data;
    if (data.detail) return data.detail;
    return Object.keys(data).map(function (key) {
      var value = data[key];
      if (Array.isArray(value)) {
        value = value.map(function (v) { return typeof v === 'object' && v !== null ? JSON.stringify(v) : v; }).join(' ');
      } else if (typeof value === 'object' && value !== null) {
        value = JSON.stringify(value);
      }
      return key === 'non_field_errors' ? value : key + ': ' + value;
    }).join(' | ');
  }

  function request(method, path, options) {
    options = options || {};
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open(method, buildUrl(path, options.params), true);
      xhr.setRequestHeader('Accept', 'application/json');
      if (options.body !== undefined) xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.timeout = 15000;

      xhr.onload = function () {
        var data = null;
        if (xhr.responseText) {
          try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(data);
        } else {
          var error = new Error(formatErrors(data, xhr.status));
          error.status = xhr.status;
          error.data = data;
          reject(error);
        }
      };
      xhr.onerror = function () { reject(new Error('Network error: could not reach the API.')); };
      xhr.ontimeout = function () { reject(new Error('The API request timed out.')); };
      xhr.send(options.body !== undefined ? JSON.stringify(options.body) : null);
    });
  }

  global.Api = {
    get: function (path, params) { return request('GET', path, { params: params }); },
    post: function (path, body) { return request('POST', path, { body: body }); },
    patch: function (path, body) { return request('PATCH', path, { body: body }); },
    del: function (path) { return request('DELETE', path); }
  };
})(window);
