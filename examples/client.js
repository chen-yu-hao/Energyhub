// Serve /api/energyhub through the same origin (and existing authentication).
export function energyhubClient(base = '/api/energyhub') {
  async function json(path, options) {
    const response = await fetch(base + path, options);
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  }
  return {
    methods: () => json('/methods'), // [{name, available, reason, ...}]
    config: () => json('/config'),
    configure: (pool_size, memory_pool_mb) => json('/config', {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pool_size, memory_pool_mb}),
    }),
    submit: (tgz, ref, options = {}) => {
      const form = new FormData();
      form.append('tgz', tgz);
      form.append('ref', ref);
      for (const [name, value] of Object.entries(options)) form.append(name, String(value));
      return json('/jobs', {method: 'POST', body: form});
    },
    status: (id) => json(`/jobs/${encodeURIComponent(id)}`),
    cancel: (id) => json(`/jobs/${encodeURIComponent(id)}/cancel`, {method: 'POST'}),
    downloadUrl: (id) => `${base}/jobs/${encodeURIComponent(id)}/result`,
  };
}
