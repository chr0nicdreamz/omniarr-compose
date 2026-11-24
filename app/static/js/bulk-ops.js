        function updateBulkMoveBar() {
            const cbs = Array.from(document.querySelectorAll('.bulk-item-cb:checked'));
            const bar = document.getElementById('bulk-move-bar');
            const cnt = document.getElementById('bulk-move-count');
            if (!bar || !cnt) return;
            if (cbs.length === 0) {
                bar.classList.add('hidden');
            } else {
                bar.classList.remove('hidden');
                cnt.textContent = `${cbs.length} title(s) selected`;
            }
        }
        function clearBulkSelection() {
            document.querySelectorAll('.bulk-item-cb:checked').forEach(cb => cb.checked = false);
            updateBulkMoveBar();
        }
        async function executeBulkMove() {
            const ids = Array.from(document.querySelectorAll('.bulk-item-cb:checked')).map(cb => parseInt(cb.value, 10));
            const target = document.getElementById('bulk-move-target').value;
            if (!ids.length || !target) {
                alert('Select at least one title and a destination root drive.');
                return;
            }
            if (!confirm(`Move ${ids.length} folder(s) to ${target} in the background?`)) return;
            const res = await fetch('/api/library/bulk-move', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({item_ids: ids, target_root: target})
            });
            const data = await res.json();
            if (!data.ok) {
                alert(data.error || 'Failed to start bulk move.');
                return;
            }
            clearBulkSelection();
            if (typeof pollBackgroundTasks === 'function') pollBackgroundTasks();
        }
        async function loadDriveStats() {
            const grid = document.getElementById('drive-storage-grid');
            if (!grid) return;
            try {
                const res = await fetch('/api/storage/drives');
                const data = await res.json();
                const drives = data.drives || [];
                if (!drives.length) {
                    grid.innerHTML = '<div class="text-slate-500">No mounted root drives found.</div>';
                    return;
                }
                grid.innerHTML = drives.map(d => {
                    const barColor = d.used_pct > 90 ? 'bg-rose-500' : (d.used_pct > 75 ? 'bg-amber-400' : 'bg-emerald-500');
                    return `<div class="bg-slate-950 border border-slate-800 rounded-lg p-3">
                        <div class="flex justify-between font-semibold text-slate-200 mb-1">
                            <span class="truncate" title="${d.local_path}">${d.local_path}</span>
                            <span class="text-emerald-300 ml-2 whitespace-nowrap">${d.free_gb} GB free</span>
                        </div>
                        <div class="w-full h-2 bg-slate-800 rounded-full overflow-hidden mb-1.5">
                            <div class="${barColor} h-full" style="width:${d.used_pct}%"></div>
                        </div>
                        <div class="flex justify-between text-[11px] text-slate-400">
                            <span>${d.used_gb} / ${d.total_gb} GB (${d.used_pct}%)</span>
                            <span>${d.item_count} titles</span>
                        </div>
                    </div>`;
                }).join('');
            } catch (e) {}
        }
        loadDriveStats();

        async function triggerRematchAllPosters() {
            if (!confirm('Clear all cached posters and rematch the entire library from Plex, local assets, and metadata APIs?')) return;
            const res = await fetch('/api/posters/rematch-all', {method: 'POST'});
            const data = await res.json();
            if (!data.ok) {
                alert(data.error || 'Poster rematch already running.');
                return;
            }
            if (typeof pollBackgroundTasks === 'function') pollBackgroundTasks();
        }



        async function loadDedicatedTautulliTab() {
            const liveGrid = document.getElementById('tt-live-grid');
            if (!liveGrid) return;
            try {
                const res = await fetch('/api/tautulli/overview');
                const data = await res.json();
                const act = data.activity || {};
                const stats = data.stats || {};
                const statusBox = document.getElementById('tautulli-tab-status');

                if (!act.configured) {
                    statusBox.innerHTML = `<div class="bg-amber-950/40 border border-amber-500/40 text-amber-200 rounded-xl p-4 text-xs">
                        Tautulli is not configured yet. Add your <strong>Tautulli URL</strong> and <strong>Tautulli API Key</strong> under <a href="/?tab=settings&section=connect" class="underline font-semibold text-white">Settings → Connect &amp; Backup</a>.
                    </div>`;
                    liveGrid.innerHTML = '<div class="text-slate-500">Waiting for Tautulli connection...</div>';
                    return;
                }
                if (act.error || stats.error) {
                    statusBox.innerHTML = `<div class="bg-rose-950/40 border border-rose-500/40 text-rose-200 rounded-xl p-4 text-xs">
                        Tautulli API Error: <strong>${act.error || stats.error}</strong> — verify your Tautulli URL (e.g. <code>http://tautulli:8181</code>) and API Key under <a href="/?tab=settings&section=connect" class="underline font-semibold text-white">Settings → Connect &amp; Backup</a>.
                    </div>`;
                } else {
                    statusBox.innerHTML = '';
                }

                document.getElementById('tt-live-bw').textContent = act.total_bandwidth ? `${Math.round(act.total_bandwidth / 1000)} Mbps total bandwidth` : 'Idle';
                liveGrid.innerHTML = (act.sessions && act.sessions.length)
                    ? act.sessions.map(s => {
                        const isTranscode = String(s.transcode_decision || '').toLowerCase().includes('transcode');
                        const badge = isTranscode
                            ? `<span class="px-2 py-0.5 rounded bg-amber-950 text-amber-300 border border-amber-700/50 text-[10px] font-bold">TRANSCODE</span>`
                            : `<span class="px-2 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-700/50 text-[10px] font-bold">DIRECT PLAY</span>`;
                        return `<div class="bg-slate-950 border border-slate-800 rounded-xl p-4 space-y-2">
                            <div class="flex items-start justify-between gap-2">
                                <div class="font-bold text-slate-100 text-sm">${s.title}</div>
                                ${badge}
                            </div>
                            <div class="text-slate-400">👤 <strong class="text-slate-200">${s.user}</strong> · ${s.player} (${s.video_resolution || 'Auto'})</div>
                            <div class="w-full h-2 bg-slate-800 rounded-full overflow-hidden">
                                <div class="bg-amber-400 h-full" style="width:${s.progress_percent}%"></div>
                            </div>
                            <div class="flex justify-between text-[11px] text-slate-400">
                                <span class="capitalize">${s.state}</span>
                                <span class="font-mono text-indigo-300">${s.progress_percent}%</span>
                            </div>
                        </div>`;
                    }).join('')
                    : '<div class="col-span-full text-slate-500 py-4">No active Plex streams right now.</div>';

                const tm = stats.top_movies || [];
                document.getElementById('tt-top-movies').innerHTML = tm.length
                    ? tm.map((m, i) => `<div class="flex items-center justify-between py-1.5 border-b border-slate-800/60"><span class="truncate text-slate-200"><span class="text-slate-500 font-mono mr-1.5">#${i+1}</span>${m.title} ${m.year ? `(${m.year})` : ''}</span><span class="font-mono text-indigo-300 shrink-0 ml-2">${m.plays} plays</span></div>`).join('')
                    : '<div class="text-slate-500">No movie plays recorded in the last 30 days.</div>';

                const tt = stats.top_tv || [];
                document.getElementById('tt-top-tv').innerHTML = tt.length
                    ? tt.map((t, i) => `<div class="flex items-center justify-between py-1.5 border-b border-slate-800/60"><span class="truncate text-slate-200"><span class="text-slate-500 font-mono mr-1.5">#${i+1}</span>${t.title}</span><span class="font-mono text-emerald-300 shrink-0 ml-2">${t.plays} plays</span></div>`).join('')
                    : '<div class="text-slate-500">No TV plays recorded in the last 30 days.</div>';

                const tu = stats.top_users || [];
                document.getElementById('tt-top-users').innerHTML = tu.length
                    ? tu.map((u, i) => `<div class="flex items-center justify-between py-1.5 border-b border-slate-800/60"><span class="truncate text-slate-200"><span class="text-slate-500 font-mono mr-1.5">#${i+1}</span>${u.user}</span><span class="font-mono text-amber-300 shrink-0 ml-2">${u.plays} plays (${u.duration_hrs}h)</span></div>`).join('')
                    : '<div class="text-slate-500">No user playback stats in the last 30 days.</div>';

                const rw = stats.recent_watches || [];
                window._ttRecentWatches = rw;
                window._ttRecentExpanded = false;
                renderTautulliRecentHistory();

                const stales = stats.stale_items || [];
                window._ttStaleItems = stales;
                window._ttStaleExpanded = false;
                document.getElementById('tt-stale-count').textContent = `${stales.length} candidate(s)`;
                renderTautulliStaleTable();
            } catch (e) {}
        }
        if (document.getElementById('tt-live-grid')) loadDedicatedTautulliTab();

        function renderTautulliRecentHistory() {
            const box = document.getElementById('tt-recent-history');
            if (!box) return;
            const rw = window._ttRecentWatches || [];
            if (!rw.length) {
                box.innerHTML = '<div class="text-slate-500">No recent watch history entries returned.</div>';
                return;
            }
            const limit = 8;
            const expanded = !!window._ttRecentExpanded;
            const visible = expanded ? rw : rw.slice(0, limit);
            const rowsHtml = visible.map(w => `<tr class="border-t border-slate-800/70"><td class="py-2 text-slate-200 font-medium">${w.title}</td><td class="py-2 text-amber-300">${w.user}</td><td class="py-2 text-slate-400">${w.player}</td><td class="py-2 font-mono text-indigo-300">${w.percent_complete}%</td><td class="py-2 text-slate-400 font-mono">${w.date}</td></tr>`).join('');
            const toggleBtn = rw.length > limit
                ? `<div class="mt-3 pt-3 border-t border-slate-800/70 flex justify-center"><button type="button" onclick="window._ttRecentExpanded = !window._ttRecentExpanded; renderTautulliRecentHistory();" class="bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-200 px-4 py-1.5 rounded-lg text-xs font-semibold transition">${expanded ? `▲ Show Less (Top ${limit})` : `▼ Show All (${rw.length} Watches)`}</button></div>`
                : '';
            box.innerHTML = `<table class="w-full text-left"><thead><tr class="border-b border-slate-800 text-slate-400"><th class="py-2">Title</th><th class="py-2">User</th><th class="py-2">Client</th><th class="py-2">Progress</th><th class="py-2">Watched At</th></tr></thead><tbody>${rowsHtml}</tbody></table>${toggleBtn}`;
        }

        function renderTautulliStaleTable() {
            const box = document.getElementById('tt-dedicated-stale');
            if (!box) return;
            const stales = window._ttStaleItems || [];
            if (!stales.length) {
                box.innerHTML = '<div class="text-slate-500">All available media has been watched within the last 12 months.</div>';
                return;
            }
            const limit = 8;
            const expanded = !!window._ttStaleExpanded;
            const visible = expanded ? stales : stales.slice(0, limit);
            const rowsHtml = visible.map(s => `<tr class="border-t border-slate-800/70"><td class="py-2"><a href="/media/${s.id}" class="text-indigo-300 hover:underline font-medium">${s.title} ${s.year ? `(${s.year})` : ''}</a></td><td class="py-2 uppercase text-slate-400">${s.media_type}</td><td class="py-2 font-mono">${s.plays}</td><td class="py-2 text-amber-300">${s.last_watched}</td><td class="py-2 text-right"><a href="/media/${s.id}" class="bg-slate-800 hover:bg-slate-700 border border-slate-700 px-2.5 py-1 rounded text-[11px] text-slate-200">Manage</a></td></tr>`).join('');
            const toggleBtn = stales.length > limit
                ? `<div class="mt-3 pt-3 border-t border-slate-800/70 flex justify-center"><button type="button" onclick="window._ttStaleExpanded = !window._ttStaleExpanded; renderTautulliStaleTable();" class="bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-200 px-4 py-1.5 rounded-lg text-xs font-semibold transition">${expanded ? `▲ Show Less (Top ${limit})` : `▼ Show More (${stales.length - limit} more items)`}</button></div>`
                : '';
            box.innerHTML = `<table class="w-full text-left"><thead><tr class="border-b border-slate-800 text-slate-400"><th class="py-2">Title</th><th class="py-2">Type</th><th class="py-2">Total Plays</th><th class="py-2">Last Watched</th><th class="py-2 text-right">Action</th></tr></thead><tbody>${rowsHtml}</tbody></table>${toggleBtn}`;
        }

        async function loadFranchiseCollections() {
            const grid = document.getElementById('collections-grid');
            if (!grid) return;
            grid.innerHTML = '<div class="col-span-full text-center py-12 text-xs text-slate-400 bg-slate-900 border border-slate-800 rounded-2xl">Scanning Plex collections &amp; TMDB franchises...</div>';
            try {
                const res = await fetch('/api/collections');
                const data = await res.json();
                const cols = data.collections || [];
                window._omniCollections = cols;
                if (!cols.length) {
                    grid.innerHTML = '<div class="col-span-full text-center py-12 text-xs text-slate-400 bg-slate-900 border border-slate-800 rounded-2xl">No collections found yet. Connect Plex or add a free TMDB API key under Settings → Connect &amp; Backup to auto-discover movie franchises.</div>';
                    return;
                }
                grid.innerHTML = cols.map((c, idx) => {
                    const pct = c.total_count ? Math.round((c.owned_count / c.total_count) * 100) : 100;
                    const missing = (c.parts || []).filter(p => !p.owned && p.status === 'missing');
                    const grabBtn = missing.length
                        ? `<button type="button" onclick="grabMissingCollection(this, ${idx})" class="bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-1.5 rounded-lg text-[11px] font-semibold">🔍 Grab ${missing.length} Missing</button>`
                        : `<span class="px-2.5 py-1 rounded-lg bg-emerald-950/70 text-emerald-300 border border-emerald-700/50 text-[11px] font-semibold">✓ Complete</span>`;
                    const partsHtml = (c.parts || []).map(p => {
                        const pill = p.owned
                            ? `<span class="text-emerald-400 font-bold">✓</span>`
                            : (p.status === 'wanted' || p.status === 'downloading'
                                ? `<span class="text-amber-400 font-bold">⏳</span>`
                                : `<span class="text-rose-400 font-bold">✕</span>`);
                        const link = p.tracked_id ? `<a href="/media/${p.tracked_id}" class="hover:underline text-slate-200">${p.title}</a>` : `<span class="text-slate-400">${p.title}</span>`;
                        return `<div class="flex items-center justify-between py-1 border-t border-slate-800/70 text-xs">
                            <div class="flex items-center gap-2 truncate">${pill} ${link} ${p.year ? `<span class="text-slate-500">(${p.year})</span>` : ''}</div>
                            <span class="text-[10px] uppercase text-slate-500">${p.status}</span>
                        </div>`;
                    }).join('');
                    return `<div class="bg-slate-900 border border-slate-800 rounded-2xl p-5 flex flex-col justify-between gap-4">
                        <div>
                            <div class="flex items-start justify-between gap-3 mb-2">
                                <div>
                                    <span class="text-[10px] uppercase tracking-wider text-indigo-400 font-bold">${c.source}</span>
                                    <h3 class="text-base font-bold text-white">${c.name}</h3>
                                </div>
                                ${grabBtn}
                            </div>
                            <div class="flex items-center gap-2 text-xs text-slate-400 mb-3">
                                <div class="flex-1 h-2 bg-slate-800 rounded-full overflow-hidden">
                                    <div class="bg-indigo-500 h-full" style="width:${pct}%"></div>
                                </div>
                                <span class="font-mono text-slate-300">${c.owned_count}/${c.total_count} (${pct}%)</span>
                            </div>
                            <div class="max-h-56 overflow-y-auto pr-1">${partsHtml}</div>
                        </div>
                    </div>`;
                }).join('');
            } catch (e) {
                grid.innerHTML = '<div class="col-span-full text-center py-8 text-xs text-rose-400">Failed to scan collections.</div>';
            }
        }
        if (document.getElementById('collections-grid')) loadFranchiseCollections();

        async function grabMissingCollection(btn, idx) {
            const col = (window._omniCollections || [])[idx];
            if (!col) return;
            const missing = (col.parts || []).filter(p => !p.owned && p.status === 'missing');
            if (!missing.length) return;
            btn.disabled = true;
            btn.textContent = 'Queueing...';
            const res = await fetch('/api/collections/grab-missing', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({missing})
            });
            if (res.ok) {
                btn.className = 'bg-emerald-600 text-white px-3 py-1.5 rounded-lg text-[11px] font-semibold';
                btn.textContent = '✓ Searching Missing';
                setTimeout(loadFranchiseCollections, 1200);
            }
        }

        async function loadHealthAndTautulli(force = false) {
            const cards = document.getElementById('health-summary-cards');
            if (!cards) return;
            if (force) {
                cards.innerHTML = '<div class="bg-slate-900 border border-slate-800 rounded-xl p-4 text-slate-400 col-span-full">Running fresh library health audit...</div>';
            }
            try {
                const [hRes, tRes] = await Promise.all([
                    fetch('/api/health/audit' + (force ? '?force=true' : '')),
                    fetch('/api/tautulli/overview')
                ]);
                const h = await hRes.json();
                const t = hRes.ok ? await tRes.json() : { stats: {} };
                if (!hRes.ok) {
                    cards.innerHTML = `<div class="bg-rose-950/40 border border-rose-500/40 rounded-xl p-4 text-rose-300 col-span-full">Health audit error: ${h.detail || hRes.status}</div>`;
                    return;
                }
                const codecsStr = Object.entries(h.codecs || {}).map(([k, v]) => `${k}: ${v}`).join(' · ') || 'None';
                const resStr = Object.entries(h.resolutions || {}).map(([k, v]) => `${k}: ${v}`).join(' · ') || 'None';
                cards.innerHTML = `
                    <div class="bg-slate-900 border border-slate-800 rounded-xl p-4">
                        <div class="text-slate-400 mb-1">Video Codecs</div>
                        <div class="font-bold text-indigo-300 text-sm">${codecsStr}</div>
                    </div>
                    <div class="bg-slate-900 border border-slate-800 rounded-xl p-4">
                        <div class="text-slate-400 mb-1">Resolutions</div>
                        <div class="font-bold text-emerald-300 text-sm">${resStr}</div>
                    </div>
                    <div class="bg-slate-900 border border-slate-800 rounded-xl p-4">
                        <div class="text-slate-400 mb-1">Tdarr H.264 Candidates</div>
                        <div class="font-bold text-amber-300 text-lg">${(h.h264_candidates || []).length}</div>
                    </div>
                    <div class="bg-slate-900 border border-slate-800 rounded-xl p-4">
                        <div class="text-slate-400 mb-1">Duplicates &amp; Orphans</div>
                        <div class="font-bold text-rose-300 text-lg">${(h.duplicates || []).length} dup / ${(h.orphaned_downloads || []).length} orphan</div>
                    </div>
                `;

                const h264Box = document.getElementById('health-h264-table');
                const hList = h.h264_candidates || [];
                h264Box.innerHTML = hList.length
                    ? `<table class="w-full text-left"><thead><tr class="border-b border-slate-800 text-slate-400"><th class="py-2">Title</th><th class="py-2">Res</th><th class="py-2">Audio</th><th class="py-2">Size</th></tr></thead><tbody>${hList.map(x => `<tr class="border-t border-slate-800/70"><td class="py-2"><a href="/media/${x.id}" class="text-indigo-300 hover:underline font-medium">${x.title}</a></td><td class="py-2">${x.resolution}</td><td class="py-2">${x.audio}</td><td class="py-2 font-mono text-amber-300">${x.size_gb} GB</td><td class="py-2 text-right"><button type="button" onclick="queueItemTdarr(${x.id}, this)" class="bg-indigo-600/30 hover:bg-indigo-600/60 border border-indigo-500/40 text-indigo-200 px-2.5 py-1 rounded text-[11px]">⚡ Queue Tdarr</button></td></tr>`).join('')}</tbody></table>`
                    : '<div class="text-slate-500">No large H.264 files found—your library is already well compressed!</div>';

                const dupBox = document.getElementById('health-duplicates-list');
                const dups = h.duplicates || [];
                dupBox.innerHTML = dups.length
                    ? dups.map(d => `<div class="bg-slate-950 border border-slate-800 rounded-lg p-3"><a href="/media/${d.id}" class="font-bold text-amber-300 hover:underline">${d.title}</a><div class="mt-1 font-mono text-[11px] text-slate-400">${d.files.join('<br>')}</div></div>`).join('')
                    : '<div class="text-slate-500">No duplicate movie files detected.</div>';

                const orphBox = document.getElementById('health-orphans-list');
                const orphs = h.orphaned_downloads || [];
                orphBox.innerHTML = orphs.length
                    ? orphs.map(o => `<div class="bg-slate-950 border border-slate-800 rounded-lg p-3 flex items-center justify-between gap-2"><div class="truncate"><div class="font-mono text-slate-200 truncate" title="${o.path}">${o.name}</div><div class="text-[11px] text-slate-500">${o.size_gb} GB</div></div><button type="button" data-orphan-path="${encodeURIComponent(o.path)}" onclick="deleteOrphanFile(this, decodeURIComponent(this.dataset.orphanPath || ''))" class="shrink-0 bg-rose-600/20 hover:bg-rose-600/40 border border-rose-500/40 text-rose-300 px-2.5 py-1 rounded text-[11px]">Delete</button></div>`).join('')
                    : '<div class="text-slate-500">No orphaned video downloads found.</div>';

                const staleBox = document.getElementById('tautulli-stale-list');
                const stats = t.stats || {};
                if (!stats.configured) {
                    staleBox.innerHTML = '<div class="text-slate-500">Configure Tautulli URL and API Key under <a href="/?tab=settings&section=connect" class="text-indigo-400 underline">Settings → Connect &amp; Backup</a> to surface watch counts and stale media.</div>';
                } else {
                    const stales = stats.stale_items || [];
                    staleBox.innerHTML = stales.length
                        ? `<table class="w-full text-left"><thead><tr class="border-b border-slate-800 text-slate-400"><th class="py-2">Title</th><th class="py-2">Type</th><th class="py-2">Plays</th><th class="py-2">Last Watched</th></tr></thead><tbody>${stales.map(s => `<tr class="border-t border-slate-800/70"><td class="py-2"><a href="/media/${s.id}" class="text-slate-200 hover:underline font-medium">${s.title}</a></td><td class="py-2 uppercase text-slate-400">${s.media_type}</td><td class="py-2 font-mono">${s.plays}</td><td class="py-2 text-amber-300">${s.last_watched}</td></tr>`).join('')}</tbody></table>`
                        : '<div class="text-slate-500">Every available item in your library has been watched within the last 12 months!</div>';
                }
            } catch (e) {}
        }
        if (document.getElementById('health-summary-cards')) loadHealthAndTautulli();

        async function deleteOrphanFile(btn, fpath) {
            if (!confirm(`Permanently delete orphaned download:
${fpath}?`)) return;
            btn.disabled = true;
            const res = await fetch('/api/health/delete-orphan', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path: fpath})
            });
            if (res.ok) btn.closest('div').remove();
            else { alert('Failed to delete orphan file'); btn.disabled = false; }
        }
