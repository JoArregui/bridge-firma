const canvas = document.getElementById('canvas-firma');
const ctx = canvas.getContext('2d');
let dibujando = false;

// Configuración del trazo
ctx.lineWidth = 3;
ctx.lineJoin = 'round';
ctx.lineCap = 'round';
ctx.strokeStyle = '#000000';

// Eventos Ratón
canvas.addEventListener('mousedown', (e) => { dibujando = true; [lastX, lastY] = [e.offsetX, e.offsetY]; });
canvas.addEventListener('mousemove', dibujar);
canvas.addEventListener('mouseup', () => dibujando = false);

// Eventos Táctiles (Tablet)
canvas.addEventListener('touchstart', (e) => {
    e.preventDefault();
    dibujando = true;
    const touch = e.touches[0];
    const rect = canvas.getBoundingClientRect();
    [lastX, lastY] = [touch.clientX - rect.left, touch.clientY - rect.top];
});

canvas.addEventListener('touchmove', (e) => {
    e.preventDefault();
    if (!dibujando) return;
    const touch = e.touches[0];
    const rect = canvas.getBoundingClientRect();
    const x = touch.clientX - rect.left;
    const y = touch.clientY - rect.top;
    
    ctx.beginPath();
    ctx.moveTo(lastX, lastY);
    ctx.lineTo(x, y);
    ctx.stroke();
    [lastX, lastY] = [x, y];
});

canvas.addEventListener('touchend', () => dibujando = false);

function dibujar(e) {
    if (!dibujando) return;
    ctx.beginPath();
    ctx.moveTo(lastX, lastY);
    ctx.lineTo(e.offsetX, e.offsetY);
    ctx.stroke();
    [lastX, lastY] = [e.offsetX, e.offsetY];
}

function limpiarCanvas() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
}

function enviarFirma(documentoId) {
    const dataURL = canvas.toDataURL('image/png');
    
    // Verificación básica: no enviar si está vacío
    if (dataURL.length < 2000) { // Un canvas vacío suele ser pequeño en string
        alert("Por favor, firme el documento antes de confirmar.");
        return;
    }

    fetch('/guardar_firma', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            id: documentoId,
            firma_b64: dataURL
        })
    })
    .then(res => res.json())
    .then(data => {
        if (data.status === 'success') {
            alert("Firma guardada correctamente.");
            window.location.href = "/"; // Volver a la lista o siguiente doc
        } else {
            alert("Error: " + data.message);
        }
    });
}