        function setOmniGridCols(cols) {
            localStorage.setItem('omni_grid_cols', cols);
            const map = {
                'auto': 'omni-poster-grid grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 lg:grid-cols-6 xl:grid-cols-8 2xl:grid-cols-10 gap-3.5',
                '6':    'omni-poster-grid grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-3.5',
                '8':    'omni-poster-grid grid grid-cols-2 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 gap-3',
                '10':   'omni-poster-grid grid grid-cols-3 sm:grid-cols-5 md:grid-cols-8 lg:grid-cols-10 gap-2.5',
                '12':   'omni-poster-grid grid grid-cols-3 sm:grid-cols-6 md:grid-cols-9 lg:grid-cols-12 gap-2.5'
            };
            const targetCls = map[cols] || map['auto'];
            document.querySelectorAll('.omni-poster-grid').forEach(g => { g.className = targetCls; });
            document.querySelectorAll('.omni-grid-btn').forEach(btn => {
                if (btn.dataset.gridCols === cols) {
                    btn.classList.add('bg-indigo-600', 'text-white');
                    btn.classList.remove('text-slate-300', 'hover:bg-slate-800');
                } else {
                    btn.classList.remove('bg-indigo-600', 'text-white');
                    btn.classList.add('text-slate-300', 'hover:bg-slate-800');
                }
            });
        }
        document.addEventListener('DOMContentLoaded', () => {
            const savedCols = localStorage.getItem('omni_grid_cols') || 'auto';
            setOmniGridCols(savedCols);
        });
