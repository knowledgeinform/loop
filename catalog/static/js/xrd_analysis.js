(function () {
  'use strict';

  function getCookie(name) {
    var cookieValue = null;
    if (!document.cookie) return cookieValue;
    document.cookie.split(';').forEach(function (cookie) {
      var trimmed = cookie.trim();
      if (trimmed.substring(0, name.length + 1) === name + '=') {
        cookieValue = decodeURIComponent(trimmed.substring(name.length + 1));
      }
    });
    return cookieValue;
  }

  function parseJson(response) {
    return response.json().catch(function () {
      return {};
    });
  }

  function bindPlotToggles() {
    document.querySelectorAll('[data-xrd-plot-toggle]').forEach(function (toggle) {
      toggle.addEventListener('change', function () {
        var seriesId = toggle.getAttribute('data-target-series');
        document.querySelectorAll('[data-xrd-series="' + seriesId + '"]').forEach(function (node) {
          node.style.display = toggle.checked ? '' : 'none';
        });
      });
    });
  }

  function bindTrialAnalysisCard(card) {
    var submitButton = card.querySelector('[data-xrd-submit-button]');
    var statusBadge = card.querySelector('[data-xrd-status-badge]');
    var statusMessage = card.querySelector('[data-xrd-status-message]');
    var analysisIdNode = card.querySelector('[data-xrd-analysis-id]');
    var detailLink = card.querySelector('[data-xrd-detail-link]');
    var submitUrl = card.getAttribute('data-submit-url');
    var initialStatusUrl = card.getAttribute('data-status-url');
    var pollHandle = null;

    function setStatus(label, badgeClass, message) {
      if (statusBadge) {
        statusBadge.textContent = label;
        statusBadge.className = 'badge ' + badgeClass;
      }
      if (statusMessage && message) {
        statusMessage.textContent = message;
      }
    }

    function stopPolling() {
      if (pollHandle) {
        window.clearInterval(pollHandle);
        pollHandle = null;
      }
    }

    function startPolling(statusUrl) {
      if (!statusUrl) return;
      stopPolling();
      pollHandle = window.setInterval(function () {
        fetch(statusUrl, { credentials: 'same-origin' })
          .then(parseJson)
          .then(function (payload) {
            var data = payload.data || {};
            if (!data.status) return;
            if (data.analysis_id && analysisIdNode) {
              analysisIdNode.textContent = data.analysis_id;
            }
            if (data.status === 'queued') {
              setStatus('Queued', 'bg-info text-dark', data.progress_message || 'Queued for background analysis.');
              return;
            }
            if (data.status === 'running') {
              setStatus('Running', 'bg-primary', data.progress_message || 'Automated analysis is running.');
              return;
            }
            stopPolling();
            window.location.reload();
          })
          .catch(function () {
            stopPolling();
          });
      }, 5000);
    }

    if (initialStatusUrl && card.getAttribute('data-job-status') && card.getAttribute('data-job-status') !== 'succeeded' && card.getAttribute('data-job-status') !== 'failed') {
      startPolling(initialStatusUrl);
    }

    if (!submitButton || !submitUrl) return;
    submitButton.addEventListener('click', function () {
      if (submitButton.disabled) return;
      submitButton.disabled = true;
      setStatus('Queued', 'bg-info text-dark', 'Submitting automated XRD analysis job.');
      fetch(submitUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'X-CSRFToken': getCookie('csrftoken'),
          'Accept': 'application/json'
        }
      })
        .then(function (response) {
          return parseJson(response).then(function (payload) {
            return { response: response, payload: payload };
          });
        })
        .then(function (result) {
          if (!result.response.ok) {
            throw new Error(((result.payload.errors || [])[0] || {}).detail || 'Unable to submit automated XRD analysis.');
          }
          var data = result.payload.data || {};
          if (analysisIdNode && data.analysis_id) {
            analysisIdNode.textContent = data.analysis_id;
          }
          if (detailLink && data.analysis_id) {
            detailLink.href = detailLink.href && detailLink.href !== '#' ? detailLink.href : card.getAttribute('data-detail-url') || '#';
            detailLink.classList.remove('disabled');
            detailLink.removeAttribute('aria-disabled');
          }
          if (data.result_available) {
            window.location.reload();
            return;
          }
          setStatus(
            data.status === 'running' ? 'Running' : 'Queued',
            data.status === 'running' ? 'bg-primary' : 'bg-info text-dark',
            data.progress_message || 'Automated analysis job accepted.'
          );
          startPolling(data.status_url);
        })
        .catch(function (error) {
          submitButton.disabled = false;
          setStatus('Submission failed', 'bg-danger', error.message);
        });
    });

    window.addEventListener('beforeunload', stopPolling);
  }

  function bindDetailPolling(container) {
    var statusUrl = container.getAttribute('data-status-url');
    var jobStatus = container.getAttribute('data-job-status');
    var statusBadge = container.querySelector('[data-xrd-status-badge]');
    var statusMessage = container.querySelector('[data-xrd-status-message]');
    var pollHandle = null;
    if (!statusUrl || jobStatus === 'succeeded' || jobStatus === 'failed') return;

    function stopPolling() {
      if (pollHandle) {
        window.clearInterval(pollHandle);
        pollHandle = null;
      }
    }

    pollHandle = window.setInterval(function () {
      fetch(statusUrl, { credentials: 'same-origin' })
        .then(parseJson)
        .then(function (payload) {
          var data = payload.data || {};
          if (!data.status) return;
          if (statusBadge) {
            statusBadge.textContent = data.status === 'running' ? 'Running' : 'Queued';
            statusBadge.className = 'badge ' + (data.status === 'running' ? 'bg-primary' : 'bg-info text-dark');
          }
          if (statusMessage && data.progress_message) {
            statusMessage.textContent = data.progress_message;
          }
          if (data.status === 'failed' || data.status === 'succeeded') {
            stopPolling();
            window.location.reload();
          }
        })
        .catch(stopPolling);
    }, 5000);

    window.addEventListener('beforeunload', stopPolling);
  }

  document.addEventListener('DOMContentLoaded', function () {
    bindPlotToggles();
    document.querySelectorAll('[data-xrd-analysis-card]').forEach(bindTrialAnalysisCard);
    document.querySelectorAll('[data-xrd-analysis-detail]').forEach(bindDetailPolling);
  });
})();
