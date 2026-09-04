(() => {
	const page = document.querySelector('.history-page');
	const historyUrl = page.dataset.historyUrl;
	const svgNamespace = 'http://www.w3.org/2000/svg';
	let selectedHours = 24;

	function svgElement(name, attributes = {}, text = '') {
		const element = document.createElementNS(svgNamespace, name);
		Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value));
		if (text) element.textContent = text;
		return element;
	}

	function formatTime(timestamp, includeDate = true) {
		return new Date(timestamp * 1000).toLocaleString('de-CH', {
			day: includeDate ? '2-digit' : undefined,
			month: includeDate ? '2-digit' : undefined,
			hour: '2-digit',
			minute: '2-digit',
			second: includeDate ? '2-digit' : undefined
		});
	}

	function renderPlot(svg, readings, field, fixedRange = null) {
		const width = Math.max(Math.round(svg.getBoundingClientRect().width), 360);
		const height = width < 600 ? 220 : 280;
		const margin = { top: 18, right: 22, bottom: 42, left: 58 };
		const plotWidth = width - margin.left - margin.right;
		const plotHeight = height - margin.top - margin.bottom;
		svg.replaceChildren();
		svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
		svg.setAttribute('height', height);

		if (!readings.length) {
			svg.appendChild(svgElement('text', {
				x: width / 2,
				y: height / 2,
				class: 'plot-empty',
				'text-anchor': 'middle'
			}, 'Keine Messwerte'));
			return;
		}

		const firstTime = readings[0].timestamp;
		const lastTime = readings[readings.length - 1].timestamp;
		const timeSpan = Math.max(lastTime - firstTime, 1);
		const values = readings.map(reading => Number(reading[field]));
		const highestValue = values.reduce((highest, value) => Math.max(highest, value), 0);
		const minimum = fixedRange?.minimum ?? 0;
		const maximum = fixedRange?.maximum ?? Math.max(highestValue * 1.1, 0.1);
		const valueSpan = maximum - minimum;

		for (let index = 0; index <= 4; index += 1) {
			const y = margin.top + (plotHeight * index / 4);
			const value = minimum + valueSpan * (1 - index / 4);
			svg.appendChild(svgElement('line', {
				x1: margin.left,
				y1: y,
				x2: width - margin.right,
				y2: y,
				class: 'plot-grid'
			}));
			svg.appendChild(svgElement('text', {
				x: margin.left - 10,
				y: y + 4,
				class: 'plot-label',
				'text-anchor': 'end'
			}, value.toFixed(2)));
		}

		const points = readings.map(reading => {
			const x = margin.left + ((reading.timestamp - firstTime) / timeSpan) * plotWidth;
			const ratio = (Number(reading[field]) - minimum) / valueSpan;
			const y = margin.top + (1 - ratio) * plotHeight;
			return `${x.toFixed(1)},${y.toFixed(1)}`;
		}).join(' ');

		const stroke = field === 'battery_voltage' ? '#16836f' : '#d94841';
		svg.appendChild(svgElement('polyline', {
			points,
			class: `plot-line plot-line-${field}`,
			fill: 'none',
			stroke,
			'stroke-width': 2.5,
			'stroke-linecap': 'round',
			'stroke-linejoin': 'round'
		}));

		if (readings.length <= 120) {
			readings.forEach(reading => {
				const x = margin.left + ((reading.timestamp - firstTime) / timeSpan) * plotWidth;
				const ratio = (Number(reading[field]) - minimum) / valueSpan;
				const y = margin.top + (1 - ratio) * plotHeight;
				svg.appendChild(svgElement('circle', {
					cx: x,
					cy: y,
					r: 2.5,
					class: `plot-point plot-point-${field}`,
					fill: stroke
				}));
			});
		}

		const includeDate = selectedHours > 24;
		svg.appendChild(svgElement('text', {
			x: margin.left,
			y: height - 12,
			class: 'plot-label',
			'text-anchor': 'start'
		}, formatTime(firstTime, includeDate)));
		svg.appendChild(svgElement('text', {
			x: width - margin.right,
			y: height - 12,
			class: 'plot-label',
			'text-anchor': 'end'
		}, formatTime(lastTime, includeDate)));
	}

	function renderLog(readings) {
		const log = document.getElementById('history-log');
		const empty = document.getElementById('history-empty');
		log.replaceChildren();
		empty.hidden = readings.length !== 0;

		readings.slice(0, 250).forEach(reading => {
			const row = document.createElement('tr');
			const values = [
				formatTime(reading.timestamp),
				`${Number(reading.rms).toFixed(3)} m/s²`,
				`${Number(reading.dominant_freq).toFixed(1)} Hz`,
				`${Number(reading.battery_voltage).toFixed(2)} V`
			];
			values.forEach(value => {
				const cell = document.createElement('td');
				cell.textContent = value;
				row.appendChild(cell);
			});
			log.appendChild(row);
		});
	}

	async function loadHistory() {
		const error = document.getElementById('history-error');
		try {
			const response = await fetch(`${historyUrl}?hours=${selectedHours}`, { cache: 'no-store' });
			if (!response.ok) throw new Error(`HTTP ${response.status}`);
			const data = await response.json();
			const newestFirst = data.readings;
			const chronological = [...newestFirst].reverse();
			const latest = newestFirst[0];

			document.getElementById('reading-count').textContent = `${newestFirst.length} Messungen`;
			document.getElementById('history-update-time').textContent = new Date().toLocaleString('de-CH');
			document.getElementById('latest-rms').textContent = latest ? `${Number(latest.rms).toFixed(3)} m/s²` : '–';
			document.getElementById('latest-frequency').textContent = latest ? `${Number(latest.dominant_freq).toFixed(1)} Hz` : '–';
			document.getElementById('latest-battery').textContent = latest ? `${Number(latest.battery_voltage).toFixed(2)} V` : '–';

			renderPlot(document.getElementById('rms-plot'), chronological, 'rms');
			renderPlot(document.getElementById('battery-plot'), chronological, 'battery_voltage', {
				minimum: 1.0,
				maximum: 3.55
			});
			renderLog(newestFirst);
			error.hidden = true;
		} catch (exception) {
			error.textContent = `Messverlauf konnte nicht geladen werden: ${exception.message}`;
			error.hidden = false;
		}
	}

	document.querySelectorAll('[data-hours]').forEach(button => {
		button.addEventListener('click', () => {
			selectedHours = Number(button.dataset.hours);
			document.querySelectorAll('[data-hours]').forEach(candidate => {
				const active = candidate === button;
				candidate.classList.toggle('active', active);
				candidate.setAttribute('aria-pressed', active ? 'true' : 'false');
			});
			loadHistory();
		});
	});

	loadHistory();
	window.setInterval(loadHistory, 5000);
})();
