        let _msCurrentItemId = null;
        function closeManualSearch() {
            document.getElementById('manual-search-modal').classList.add('hidden');
        }
        async function openManualSearch(itemId, title) {
            _msCurrentItemId = itemId;
            document.getElementById('ms-modal-title').textContent = `Releases: ${title}`;
            document.getElementById('ms-custom-query').value = '';
            document.getElementById('manual-search-modal').classList.remove('hidden');
            await runManualSearchQuery();
        }
        async function runManualSearchQuery() {
            const body = document.getElementById('ms-modal-body');
            const q = document.getElementById('ms-custom-query').value.trim();
            body.innerHTML = '<div class="text-slate-400 text-center py-8">Querying Prowlarr indexers...</div>';
            try {
                const res = await fetch(`/api/search/manual/${_msCurrentItemId}?q=${encodeURIComponent(q)}`);
                const data = await res.json();
                if (data.error) {
                    body.innerHTML = `<div class="text-rose-400 text-center py-8">Prowlarr error: ${data.error}</div>`;
                    return;
                }
                if (!data.releases || data.releases.length === 0) {
                    body.innerHTML = '<div class="text-slate-400 text-center py-8">No releases returned by Prowlarr. Try a custom search override above.</div>';
                    return;
                }
                let rows = data.releases.map((r, idx) => {
                    const warnHtml = (r.warnings && r.warnings.length)
                        ? `<span class="ml-1.5 px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300 text-[10px]">${r.warnings.join(', ')}</span>`
                        : '';
                    return `<tr class="border-b border-slate-800/80 hover:bg-slate-800/40">
                        <td class="py-2 pr-2 font-mono text-[11px] text-slate-200 break-all">${r.title}${warnHtml}</td>
                        <td class="py-2 px-2 text-slate-400 whitespace-nowrap">${r.indexer}</td>
                        <td class="py-2 px-2 text-slate-300 whitespace-nowrap">${r.size_gb} GB</td>
                        <td class="py-2 px-2 text-emerald-400 whitespace-nowrap">${r.seeders} / ${r.peers}</td>
                        <td class="py-2 px-2 text-indigo-300 font-semibold whitespace-nowrap">${r.score}</td>
                        <td class="py-2 pl-2 text-right whitespace-nowrap">
                            <button type="button" onclick="grabManualRelease(this, ${idx})"
                                    class="bg-indigo-600 hover:bg-indigo-500 text-white px-2.5 py-1 rounded text-[11px] font-medium">Grab</button>
                        </td>
                    </tr>`;
                }).join('');
                window._msLastReleases = data.releases;
                body.innerHTML = `<table class="w-full text-left border-collapse">
                    <thead>
                        <tr class="border-b border-slate-700 text-slate-400 text-[11px]">
                            <th class="pb-2">Release</th>
                            <th class="pb-2 px-2">Indexer</th>
                            <th class="pb-2 px-2">Size</th>
                            <th class="pb-2 px-2">Seed/Peer</th>
                            <th class="pb-2 px-2">Score</th>
                            <th class="pb-2 pl-2 text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>`;
            } catch (err) {
                body.innerHTML = `<div class="text-rose-400 text-center py-8">Search failed: ${err}</div>`;
            }
        }
        async function grabManualRelease(btn, idx) {
            const rel = window._msLastReleases[idx];
            if (!rel) return;
            btn.disabled = true;
            btn.textContent = 'Sending...';
            try {
                const res = await fetch('/api/grab/manual', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({item_id: _msCurrentItemId, download_url: rel.download_url})
                });
                const data = await res.json();
                if (data.ok) {
                    btn.className = 'bg-emerald-600 text-white px-2.5 py-1 rounded text-[11px] font-medium';
                    btn.textContent = '✓ Grabbed';
                } else {
                    btn.textContent = 'Failed';
                }
            } catch (e) {
                btn.textContent = 'Error';
            }
        }

        async function loadActivityTab() {
            const box = document.getElementById('activity-queue-box');
            const blBox = document.getElementById('blacklist-box');
            if (!box) return;
            try {
                const res = await fetch('/api/activity');
                const data = await res.json();
                if (data.error) {
                    box.innerHTML = `<div class="text-rose-400">qBittorrent error: ${data.error}</div>`;
                    return;
                }
                if (!data.torrents || data.torrents.length === 0) {
                    box.innerHTML = '<div class="text-slate-400 py-4">No active torrents in OmniArr category.</div>';
                } else {
                    const rows = data.torrents.map(t => `
                        <tr class="border-b border-slate-800/80">
                            <td class="py-2.5 pr-3">
                                <div class="font-mono text-slate-200 break-all">${t.name}</div>
                                ${t.media_title ? `<div class="text-[11px] mt-0.5"><a href="/media/${t.media_id}" class="text-indigo-400 hover:text-indigo-300 underline">Linked: ${t.media_title} →</a></div>` : ''}
                            </td>
                            <td class="py-2.5 px-2 whitespace-nowrap text-slate-300">${t.size_gb} GB</td>
                            <td class="py-2.5 px-2 whitespace-nowrap">
                                <div class="w-28 bg-slate-800 rounded-full h-2 overflow-hidden">
                                    <div class="bg-indigo-500 h-2" style="width:${t.progress}%"></div>
                                </div>
                                <div class="text-[10px] text-slate-400 mt-0.5">${t.progress}% • ${t.dlspeed_mb} MB/s</div>
                            </td>
                            <td class="py-2.5 px-2 whitespace-nowrap">
                                <span class="px-2 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-300">${t.state}</span>
                            </td>
                            <td class="py-2.5 pl-2 text-right whitespace-nowrap space-x-1">
                                <button type="button" data-hash="${t.hash}" data-mid="${t.media_id || ''}"
                                        onclick="forceImportTorrent(this)"
                                        class="bg-emerald-600/20 hover:bg-emerald-600/30 border border-emerald-500/40 text-emerald-300 px-2.5 py-1 rounded text-[11px]">
                                    ⬇ Force Import
                                </button>
                                <button type="button" data-hash="${t.hash}" data-name="${t.name}" data-mid="${t.media_id || ''}"
                                        onclick="blacklistTorrent(this)"
                                        class="bg-rose-600/20 hover:bg-rose-600/30 border border-rose-500/40 text-rose-300 px-2.5 py-1 rounded text-[11px]">
                                    Blacklist &amp; Retry
                                </button>
                            </td>
                        </tr>`).join('');
                    box.innerHTML = `<table class="w-full text-left border-collapse">
                        <thead><tr class="border-b border-slate-700 text-slate-400 text-[11px]">
                            <th class="pb-2">Torrent</th><th class="pb-2 px-2">Size</th><th class="pb-2 px-2">Progress</th><th class="pb-2 px-2">State</th><th class="pb-2 pl-2 text-right">Action</th>
                        </tr></thead><tbody>${rows}</tbody></table>`;
                }
                if (blBox) {
                    if (!data.blacklist || data.blacklist.length === 0) {
                        blBox.innerHTML = '<div class="text-slate-500">No blacklisted releases yet.</div>';
                    } else {
                        blBox.innerHTML = '<ul class="space-y-1.5">' + data.blacklist.map(b =>
                            `<li class="flex justify-between bg-slate-800/50 px-3 py-1.5 rounded"><span class="font-mono text-slate-300 truncate pr-3">${b.title}</span><span class="text-slate-500 whitespace-nowrap">${b.reason} • ${b.date}</span></li>`
                        ).join('') + '</ul>';
                    }
                }
            } catch (e) {
                box.innerHTML = `<div class="text-rose-400">Failed to load activity: ${e}</div>`;
            }
        }
        async function forceImportTorrent(btn) {
            btn.disabled = true;
            btn.textContent = 'Importing...';
            try {
                const res = await fetch('/api/activity/import', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({hash: btn.dataset.hash, media_id: btn.dataset.mid || null})
                });
                const data = await res.json();
                if (res.ok && data.ok) {
                    btn.textContent = '✓ Imported';
                    setTimeout(loadActivityTab, 1000);
                } else {
                    alert(data.detail || 'Import failed. If unlinked, use the Manual Import Scanner below.');
                    btn.disabled = false;
                    btn.textContent = '⬇ Force Import';
                }
            } catch (e) {
                alert('Error: ' + e);
                btn.disabled = false;
                btn.textContent = '⬇ Force Import';
            }
        }
        async function forceImportCard(btn, mediaId) {
            btn.disabled = true;
            btn.textContent = 'Importing...';
            try {
                const res = await fetch('/api/activity/import', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({media_id: mediaId})
                });
                const data = await res.json();
                if (res.ok && data.ok) {
                    window.location.href = '/?msg=Imported+' + data.files + '+file(s)+successfully';
                } else {
                    alert(data.detail || 'Could not auto-import. Check Activity & Manual Import tab.');
                    btn.disabled = false;
                    btn.textContent = '⬇ Import';
                }
            } catch (e) {
                alert('Error: ' + e);
                btn.disabled = false;
                btn.textContent = '⬇ Import';
            }
        }
        async function blacklistTorrent(btn) {
            if (!confirm('Remove this torrent from qBittorrent, blacklist it, and re-search for another release?')) return;
            btn.disabled = true;
            btn.textContent = 'Blacklisting...';
            await fetch('/api/activity/blacklist', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({hash: btn.dataset.hash, name: btn.dataset.name, media_id: btn.dataset.mid || null})
            });
            loadActivityTab();
        }
        async function loadManualImportFiles() {
            const box = document.getElementById('manual-import-box');
            box.innerHTML = '<div class="text-slate-400 py-3">Scanning download mount...</div>';
            const res = await fetch('/api/manual-import/scan');
            const data = await res.json();
            if (!data.files || data.files.length === 0) {
                box.innerHTML = '<div class="text-slate-400 py-3">No video files found in download mount.</div>';
                return;
            }
            const opts = data.media.map(m => `<option value="${m.id}">[${m.media_type.toUpperCase()}] ${m.title}</option>`).join('');
            const rows = data.files.map((f, i) => `
                <div class="flex flex-wrap items-center justify-between gap-2 bg-slate-800/50 border border-slate-800 p-2.5 rounded mb-2">
                    <div class="min-w-0 flex-1">
                        <div class="font-mono text-slate-200 truncate">${f.name}</div>
                        <div class="text-[11px] text-slate-500 truncate">${f.path} (${f.size_gb} GB)</div>
                    </div>
                    <div class="flex items-center gap-2">
                        <select id="mi-sel-${i}" class="bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200 max-w-[220px]">${opts}</select>
                        <button type="button" data-path="${f.path}" onclick="runManualImport(this, ${i})"
                                class="bg-emerald-600 hover:bg-emerald-500 text-white px-3 py-1 rounded text-xs font-medium">Import</button>
                    </div>
                </div>`).join('');
            box.innerHTML = rows;
        }
        async function runManualImport(btn, idx) {
            const sel = document.getElementById(`mi-sel-${idx}`);
            btn.disabled = true;
            btn.textContent = 'Importing...';
            const res = await fetch('/api/manual-import/run', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({file_path: btn.dataset.path, media_id: sel.value})
            });
            if (res.ok) {
                btn.className = 'bg-slate-700 text-emerald-300 px-3 py-1 rounded text-xs font-medium';
                btn.textContent = '✓ Imported';
            } else {
                btn.textContent = 'Failed';
            }
        }
        if (document.getElementById('activity-queue-box')) {
            loadActivityTab();
            setInterval(loadActivityTab, 10000);
        }


        async function runNormalizeTitles(btn) {
            await fetch('/api/library/normalize-titles-bg', {method: 'POST'});
            pollBackgroundTasks();
        }

        async function runDiskLibraryScan(btn) {
            await fetch('/api/library/scan-disk-bg', {method: 'POST'});
            pollBackgroundTasks();
        }
        async function testPushNotifications(btn) {
            btn.disabled = true; btn.textContent = 'Sending...';
            await fetch('/api/notifications/test', {method: 'POST'});
            alert('Test notification dispatched to all configured channels!');
            btn.disabled = false; btn.textContent = 'Send Test Alert';
        }
        async function loadHistoryAndLogs() {
            const evBox = document.getElementById('history-events-box');
            const logPre = document.getElementById('system-logs-pre');
            if (!evBox) return;
            const [hRes, lRes] = await Promise.all([fetch('/api/history'), fetch('/api/system/logs')]);
            const hData = await hRes.json();
            const lData = await lRes.json();
            if (!hData.events || !hData.events.length) {
                evBox.innerHTML = '<div class="text-slate-500 py-3">No history events recorded yet.</div>';
            } else {
                const badge = (t) => ({
                    import: 'bg-emerald-500/20 text-emerald-300',
                    move: 'bg-indigo-500/20 text-indigo-300',
                    grab: 'bg-amber-500/20 text-amber-300',
                    blacklist: 'bg-rose-500/20 text-rose-300',
                    scan: 'bg-cyan-500/20 text-cyan-300',
                }[t] || 'bg-slate-800 text-slate-300');
                evBox.innerHTML = '<div class="divide-y divide-slate-800/80">' + hData.events.map(e => `
                    <div class="py-2 flex flex-wrap items-center justify-between gap-2">
                        <div class="flex items-center gap-2 min-w-0">
                            <span class="px-2 py-0.5 rounded uppercase text-[10px] font-semibold ${badge(e.type)}">${e.type}</span>
                            ${e.media_id ? `<a href="/media/${e.media_id}" class="text-slate-200 hover:text-indigo-400 font-medium">${e.title}</a>` : `<span class="text-slate-200 font-medium">${e.title}</span>`}
                            <span class="text-slate-500 font-mono truncate">${e.detail}</span>
                        </div>
                        <span class="text-slate-500 whitespace-nowrap">${e.date}</span>
                    </div>`).join('') + '</div>';
            }
            if (logPre && lData.logs) {
                logPre.textContent = lData.logs;
                logPre.scrollTop = logPre.scrollHeight;
            }
        }
        if (document.getElementById('history-events-box')) {
            loadHistoryAndLogs();
        }


        async function startBgPosterSync() {
            await fetch('/api/posters/sync-bg', {method: 'POST'});
            pollBackgroundTasks();
        }
        async function pollBackgroundTasks() {
            const pill = document.getElementById('bg-task-pill');
            const dot = document.getElementById('bg-task-dot');
            const txt = document.getElementById('bg-task-text');
            if (!pill || !txt) return;
            try {
                const res = await fetch('/api/tasks/status');
                const data = await res.json();
                const tasks = data.tasks || [];
                const running = tasks.filter(t => t.state === 'running');
                const latest = running[0] || tasks[tasks.length - 1];
                if (!latest) {
                    pill.classList.add('hidden');
                    return;
                }
                pill.classList.remove('hidden');
                pill.classList.add('flex');
                if (running.length > 0) {
                    dot.className = 'w-2 h-2 rounded-full bg-amber-400 animate-ping';
                    txt.textContent = `${latest.label}: ${latest.detail}`;
                } else if (latest.state === 'error') {
                    dot.className = 'w-2 h-2 rounded-full bg-rose-500';
                    txt.textContent = `${latest.label} error: ${latest.detail}`;
                } else {
                    dot.className = 'w-2 h-2 rounded-full bg-emerald-400';
                    txt.textContent = `${latest.label}: ${latest.detail}`;
                }
            } catch (e) {}
        }
        pollBackgroundTasks();
        setInterval(pollBackgroundTasks, 2500);

