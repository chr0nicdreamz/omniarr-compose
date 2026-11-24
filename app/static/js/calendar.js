        let _calActiveType = localStorage.getItem('omni_cal_type') || 'all';
        let _calActiveStatus = 'all';

        function applyCalFilters() {
            document.querySelectorAll('.cal-type-btn').forEach(b => {
                if (b.dataset.calType === _calActiveType) {
                    b.classList.add('bg-indigo-600', 'text-white');
                } else {
                    b.classList.remove('bg-indigo-600', 'text-white');
                }
            });
            document.querySelectorAll('.cal-status-btn').forEach(b => {
                if (b.dataset.calStatus === _calActiveStatus) {
                    b.classList.add('bg-slate-700', 'text-white', 'ring-1', 'ring-indigo-400');
                    b.classList.remove('bg-slate-800');
                } else {
                    b.classList.remove('bg-slate-700', 'text-white', 'ring-1', 'ring-indigo-400');
                    b.classList.add('bg-slate-800');
                }
            });
            document.querySelectorAll('.cal-pill').forEach(el => {
                const matchType = (_calActiveType === 'all' || el.dataset.calKind === _calActiveType);
                const matchStatus = (_calActiveStatus === 'all' || el.dataset.calState === _calActiveStatus);
                el.style.display = (matchType && matchStatus) ? '' : 'none';
            });
            document.querySelectorAll('.cal-aired-pill').forEach(el => {
                el.style.display = (_calActiveType === 'all' || el.dataset.calKind === _calActiveType) ? '' : 'none';
            });
        }

        function setCalMediaType(kind) {
            _calActiveType = kind;
            localStorage.setItem('omni_cal_type', kind);
            applyCalFilters();
        }

        function setCalStatusFilter(status) {
            _calActiveStatus = status;
            applyCalFilters();
        }

        document.addEventListener('DOMContentLoaded', () => {
            if (document.querySelector('.cal-type-btn')) applyCalFilters();
        });
        async function syncCalendarNow() {
            try {
                await fetch('/api/calendar/sync', { method: 'POST' });
                if (typeof pollBgTasks === 'function') pollBgTasks();
            } catch (e) {}
        }
        async function huntCalendarEp(epId, btn) {
            const orig = btn.textContent;
            btn.disabled = true;
            btn.textContent = '...';
            try {
                const res = await fetch(`/api/calendar/hunt-ep/${epId}`, { method: 'POST' });
                const data = await res.json();
                btn.textContent = data.status === 'grabbed' ? '✓' : '0';
            } catch (e) {
                btn.textContent = orig;
                btn.disabled = false;
            }
        }
