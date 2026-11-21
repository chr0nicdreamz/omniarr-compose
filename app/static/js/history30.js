        let _h30Data = [];
        let _h30Filter = 'all';

        function setHist30Filter(cat, btn) {
            _h30Filter = cat;
            document.querySelectorAll('.h30-pill').forEach(b => {
                b.classList.remove('bg-indigo-600', 'text-white');
                b.classList.add('bg-slate-800');
            });
            btn.classList.remove('bg-slate-800');
            btn.classList.add('bg-indigo-600', 'text-white');
            renderHistory30d();
        }

        async function loadHistory30d() {
            try {
                const res = await fetch('/api/activity/history-30d');
                const data = await res.json();
                _h30Data = data.items || [];
                const c = data.counts || {};
                document.getElementById('h30-cnt-all').textContent = c.all || 0;
                document.getElementById('h30-cnt-searched').textContent = c.searched || 0;
                document.getElementById('h30-cnt-downloaded').textContent = c.downloaded || 0;
                document.getElementById('h30-cnt-blacklisted').textContent = c.blacklisted || 0;
                renderHistory30d();
            } catch (e) {
                document.getElementById('history-30d-box').innerHTML = '<p class="text-rose-400">Failed to load 30-day activity history.</p>';
            }
        }

        async function unblacklistItem(rawId, btn) {
            btn.disabled = true;
            btn.textContent = '...';
            try {
                const res = await fetch(`/api/activity/unblacklist/${rawId}`, { method: 'POST' });
                if (res.ok) await loadHistory30d();
            } catch (e) {
                btn.disabled = false;
                btn.textContent = 'Unblacklist';
            }
        }

        function renderHistory30d() {
            const box = document.getElementById('history-30d-box');
            if (!box) return;
            const q = (document.getElementById('hist30-search')?.value || '').trim().toLowerCase();
            const filtered = _h30Data.filter(item => {
                if (_h30Filter !== 'all' && item.category !== _h30Filter) return false;
                if (q && !(item.title.toLowerCase().includes(q) || item.detail.toLowerCase().includes(q))) return false;
                return true;
            });

            if (!filtered.length) {
                box.innerHTML = '<div class="p-6 text-center text-slate-500 border border-slate-800/80 rounded-lg">No torrent activity found in this filter for the last 30 days.</div>';
                return;
            }

            const rows = filtered.map(item => {
                let badge = '';
                if (item.category === 'searched') {
                    badge = '<span class="px-2 py-0.5 rounded-full bg-sky-950 text-sky-300 border border-sky-800/60 text-[11px] font-semibold">🔎 SEARCHED</span>';
                } else if (item.event_type === 'import') {
                    badge = '<span class="px-2 py-0.5 rounded-full bg-emerald-950 text-emerald-300 border border-emerald-800/60 text-[11px] font-semibold">✅ IMPORTED</span>';
                } else if (item.category === 'downloaded') {
                    badge = '<span class="px-2 py-0.5 rounded-full bg-indigo-950 text-indigo-300 border border-indigo-800/60 text-[11px] font-semibold">⬇️ GRABBED</span>';
                } else if (item.category === 'blacklisted') {
                    badge = '<span class="px-2 py-0.5 rounded-full bg-rose-950 text-rose-300 border border-rose-800/60 text-[11px] font-semibold">🚫 BLACKLISTED</span>';
                } else {
                    badge = `<span class="px-2 py-0.5 rounded-full bg-slate-800 text-slate-300 text-[11px]">${item.event_type.toUpperCase()}</span>`;
                }

                const titleHtml = item.media_item_id
                    ? `<a href="/media/${item.media_item_id}" class="font-semibold text-slate-100 hover:text-indigo-300">${item.title}</a>`
                    : `<span class="font-semibold text-slate-100">${item.title}</span>`;

                const actionHtml = item.category === 'blacklisted'
                    ? `<button type="button" onclick="unblacklistItem(${item.raw_id}, this)" class="text-rose-300 hover:text-rose-200 bg-rose-950/60 border border-rose-800/60 px-2.5 py-1 rounded text-[11px]">Unblacklist</button>`
                    : '';

                return `<tr class="hover:bg-slate-800/40 transition">
                    <td class="px-3 py-2.5 font-mono text-slate-400 whitespace-nowrap">${item.created_at}</td>
                    <td class="px-3 py-2.5 whitespace-nowrap">${badge}</td>
                    <td class="px-3 py-2.5">${titleHtml}<div class="text-[11px] text-slate-400 font-mono break-all">${item.detail}</div></td>
                    <td class="px-3 py-2.5 text-right whitespace-nowrap">${actionHtml}</td>
                </tr>`;
            }).join('');

            box.innerHTML = `<div class="overflow-x-auto border border-slate-800 rounded-lg max-h-[520px] overflow-y-auto">
                <table class="w-full text-left text-xs">
                    <thead class="bg-slate-950/90 text-slate-400 uppercase text-[11px] border-b border-slate-800 sticky top-0">
                        <tr>
                            <th class="px-3 py-2.5">Date / Time</th>
                            <th class="px-3 py-2.5">Action</th>
                            <th class="px-3 py-2.5">Torrent / Search Query</th>
                            <th class="px-3 py-2.5 text-right">Manage</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-800/70">${rows}</tbody>
                </table>
            </div>`;
        }

        document.addEventListener('DOMContentLoaded', () => {
            loadHistory30d();
        });
