// Simple tab switching (no auth; production should restrict admin)
const tabs = document.querySelectorAll('.tab');
const frame = document.getElementById('panelFrame');

tabs.forEach(t => {
  t.addEventListener('click', () => {
    tabs.forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    const map = {
      feature1: './feature1.html',
      feature2: './feature2.html',
      feature3: './feature3.html',
    };
    frame.setAttribute('src', map[t.dataset.tab]);
  });
});
