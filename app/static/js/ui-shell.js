        function getSelectedBulkIds() {
            return Array.from(document.querySelectorAll('.omni-bulk-cb:checked')).map(cb => parseInt(cb.value, 10));
        }
        function toggleBulkCard(cb) {
            const card = cb.closest('.media-card');
            if (card) {
                if (cb.checked) card.classList.add('ring-2', 'ring-indigo-500');
                else card.classList.remove('ring-2', 'ring-indigo-500');
            }
            const ids = getSelectedBulkIds();
            const bar = document.getElementById('omni-bulk-bar');
            const cnt = document.getElementById('omni-bulk-count');
            if (cnt) cnt.textContent = ids.length;
            if (bar) bar.classList.toggle('hidden', ids.length === 0);
        }
        function selectAllVisibleBulk() {
            document.querySelectorAll('.omni-bulk-cb').forEach(cb => {
                cb.checked = true;
                toggleBulkCard(cb);
            });
        }
        function clearAllBulk() {
            document.querySelectorAll('.omni-bulk-cb').forEach(cb => {
                cb.checked = false;
                toggleBulkCard(cb);
            });
        }
        async function execBulkAction(action, value) {
            const ids = getSelectedBulkIds();
            if (!ids.length) return;
            if ((action === 'profile' || action === 'move') && !value) {
                alert('Choose a target value first.');
                return;
            }
            if (action === 'delete' && !confirm(`Remove ${ids.length} selected items from OmniArr?`)) return;
            const res = await fetch('/api/library/bulk-action', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ids, action, value})
            });
            const data = await res.json();
            alert(data.message || (data.ok ? 'Done' : 'Failed'));
            if (data.ok && (action === 'profile' || action === 'delete')) location.reload();
            else clearAllBulk();
        }

        async function installTrashPreset(presetKey) {
            const res = await fetch('/api/automation/trash-preset/' + encodeURIComponent(presetKey), {method: 'POST'});
            const data = await res.json();
            const msg = document.getElementById('trash-preset-msg');
            if (msg) msg.textContent = data.ok ? '✓ TRaSH preset installed! Reloading...' : 'Failed';
            if (data.ok) setTimeout(() => location.reload(), 700);
        }

        async function runAutomationNow(btn) {
            const orig = btn.textContent;
            btn.disabled = true;
            btn.textContent = '⏳ Running Hunt...';
            await fetch('/api/automation/run-now', {method: 'POST'});
            setTimeout(() => {
                btn.disabled = false;
                btn.textContent = '✓ Hunt Triggered';
                setTimeout(() => { btn.textContent = orig; }, 2500);
            }, 900);
        }

        async function queueItemTdarr(mediaId, btn) {
            const orig = btn.textContent;
            btn.disabled = true;
            btn.textContent = '...';
            const res = await fetch('/api/health/queue-tdarr', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: mediaId})
            });
            const data = await res.json();
            if (data.ok) {
                btn.textContent = '✓ Queued';
                btn.className = 'bg-emerald-600/30 border border-emerald-500/40 text-emerald-200 px-2.5 py-1 rounded text-[11px]';
            } else {
                alert(data.message || 'Tdarr queue failed');
                btn.disabled = false;
                btn.textContent = orig;
            }
        }

        async function pollQbitActiveTicker() {
            try {
                const res = await fetch('/api/qbit/active');
                if (!res.ok) return;
                const data = await res.json();
                const list = data.torrents || [];
                const bar = document.getElementById('omni-qbit-ticker');
                const box = document.getElementById('omni-qbit-items');
                if (!bar || !box) return;
                if (!list.length) {
                    bar.classList.add('hidden');
                    return;
                }
                bar.classList.remove('hidden');
                box.innerHTML = list.map(t =>
                    `<span class="inline-flex items-center gap-2 text-slate-300"><span class="max-w-[220px] truncate font-medium">${t.name}</span><span class="text-emerald-400 font-mono">${t.progress}%</span><span class="text-sky-400 font-mono">${t.dlspeed_mb} MB/s</span></span>`
                ).join('<span class="text-slate-700">|</span>');
            } catch (e) {}
        }
        document.addEventListener('DOMContentLoaded', () => {
            pollQbitActiveTicker();
            setInterval(pollQbitActiveTicker, 8000);
        });

        async function refreshServiceHealth(force = false) {
            try {
                const res = await fetch('/api/health' + (force ? '?force=true' : ''));
                if (!res.ok) return;
                const data = await res.json();
                const setDot = (id, info) => {
                    const el = document.getElementById(id);
                    if (!el || !info) return;
                    el.className = 'w-2 h-2 rounded-full ' + (
                        !info.configured ? 'bg-slate-600' : (info.ok ? 'bg-emerald-400 shadow-[0_0_6px_#34d399]' : 'bg-rose-500 shadow-[0_0_6px_#f43f5e]')
                    );
                };
                setDot('dot-prowlarr', data.prowlarr);
                setDot('dot-qbit', data.qbittorrent);
                setDot('dot-media', data.media);
                const lbl = document.getElementById('lbl-media');
                if (lbl && data.media && data.media.label) lbl.textContent = data.media.label;
            } catch (e) {}
        }
        document.addEventListener('DOMContentLoaded', () => {
            refreshServiceHealth(false);
            setInterval(() => refreshServiceHealth(false), 60000);
        });

        async function testConn(service) {
            const box = document.getElementById('test-' + service);
            if (box) {
                box.className = 'text-xs text-amber-300';
                box.textContent = '⏳ Testing ' + service + ' connection...';
            }
            const payload = {};
            document.querySelectorAll('input[name]').forEach(inp => {
                if (inp.value && inp.value.trim()) payload[inp.name] = inp.value.trim();
            });
            try {
                const res = await fetch('/api/test-connection/' + encodeURIComponent(service), {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (box) {
                    box.className = 'text-xs font-medium ' + (data.ok ? 'text-emerald-400' : 'text-rose-400');
                    box.textContent = (data.ok ? '✓ ' : '✕ ') + (data.message || (data.ok ? 'Connected' : 'Failed'));
                }
                if (typeof refreshServiceHealth === 'function') refreshServiceHealth(true);
            } catch (e) {
                if (box) {
                    box.className = 'text-xs text-rose-400';
                    box.textContent = '✕ Request error: ' + e;
                }
            }
        }
