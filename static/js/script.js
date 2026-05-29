const $ = (selector) => document.querySelector(selector);

function escapeHtml(str) {
  const div = document.createElement("div");
  div.appendChild(document.createTextNode(str));
  return div.innerHTML;
}

async function refreshDashboard() {
  const feed = $("#event-feed");
  if (!feed) return;

  try {
    const [statusResponse, eventsResponse] = await Promise.all([
      fetch("/api/status"),
      fetch("/api/events"),
    ]);

    const status = await statusResponse.json();
    const events = await eventsResponse.json();

    const serverStatus = $('[data-status="server"]');
    const databaseStatus = $('[data-status="database"]');
    const cameraStatus = $('[data-status="cameras"]');
    const feedClock = $("#feed-clock");

    if (serverStatus) serverStatus.textContent = status.server;
    if (databaseStatus) databaseStatus.textContent = status.database;
    if (cameraStatus) cameraStatus.textContent = `${status.cameras} active`;
    if (feedClock) feedClock.textContent = new Date(status.timestamp).toLocaleString();

    feed.innerHTML = events
      .map((event) => {
        const when = new Date(event.timestamp).toLocaleString();
        const detail = event.detail ? ` - ${escapeHtml(event.detail)}` : "";

        return `
          <div class="event-row">
            <span>${escapeHtml(when)}</span>
            <strong>${escapeHtml(event.action)}</strong>
            <em>${escapeHtml(event.username)} - ${escapeHtml(event.ip)}${detail}</em>
          </div>
        `;
      })
      .join("");
  } catch {
    const feedClock = $("#feed-clock");
    if (feedClock) feedClock.textContent = "Waiting for server";
  }
}

setInterval(refreshDashboard, 5000);
refreshDashboard();

function smoothBox(previousBox, nextBox, amount = 0.7) {
  if (!previousBox) return nextBox;

  return {
    x: previousBox.x * amount + nextBox.x * (1 - amount),
    y: previousBox.y * amount + nextBox.y * (1 - amount),
    width: previousBox.width * amount + nextBox.width * (1 - amount),
    height: previousBox.height * amount + nextBox.height * (1 - amount),
  };
}

function boxCenter(box) {
  return {
    x: box.x + box.width / 2,
    y: box.y + box.height / 2,
  };
}

function boxDistance(firstBox, secondBox) {
  const first = boxCenter(firstBox);
  const second = boxCenter(secondBox);
  return Math.hypot(first.x - second.x, first.y - second.y);
}

function mirrorBox(box, canvasWidth) {
  return {
    x: canvasWidth - box.x - box.width,
    y: box.y,
    width: box.width,
    height: box.height,
  };
}

function drawDetectionBox(ctx, box, label, color) {
  ctx.strokeStyle = color;
  ctx.lineWidth = 4;
  ctx.strokeRect(box.x, box.y, box.width, box.height);

  const labelWidth = Math.max(150, label.length * 9);
  const labelY = Math.max(0, box.y - 30);

  ctx.fillStyle = color;
  ctx.fillRect(box.x, labelY, labelWidth, 26);

  ctx.fillStyle = "#07110e";
  ctx.font = "15px Segoe UI";
  ctx.fillText(label, box.x + 8, labelY + 18);
}

function drawMirroredVideo(ctx, video, width, height) {
  ctx.save();
  ctx.translate(width, 0);
  ctx.scale(-1, 1);
  ctx.drawImage(video, 0, 0, width, height);
  ctx.restore();
}

function updateMotionState(frame, width, height, state) {
  const step = 10;
  const threshold = 16;
  let changed = 0;
  let sampled = 0;
  let minX = width;
  let minY = height;
  let maxX = 0;
  let maxY = 0;

  if (!state.background) {
    state.background = new Float32Array(Math.ceil(width / step) * Math.ceil(height / step));
  }

  let sampleIndex = 0;

  for (let y = 0; y < height; y += step) {
    for (let x = 0; x < width; x += step) {
      const index = (y * width + x) * 4;
      const currentLuma =
        frame.data[index] * 0.299 +
        frame.data[index + 1] * 0.587 +
        frame.data[index + 2] * 0.114;
      const backgroundLuma = state.background[sampleIndex] || currentLuma;

      sampled += 1;

      if (Math.abs(currentLuma - backgroundLuma) > threshold) {
        changed += 1;
        minX = Math.min(minX, x);
        minY = Math.min(minY, y);
        maxX = Math.max(maxX, x);
        maxY = Math.max(maxY, y);
      }

      state.background[sampleIndex] = backgroundLuma * 0.93 + currentLuma * 0.07;
      sampleIndex += 1;
    }
  }

  state.frameCount += 1;

  const rawScore = sampled ? Math.min(100, (changed / sampled) * 360) : 0;
  state.score = state.score * 0.78 + rawScore * 0.22;

  if (state.frameCount < 8) {
    return { score: 0, box: null };
  }

  if (state.score < 4 || minX >= maxX || minY >= maxY) {
    return { score: state.score, box: null };
  }

  const padding = 42;
  const box = {
    x: Math.max(0, minX - padding),
    y: Math.max(0, minY - padding),
    width: Math.min(width - Math.max(0, minX - padding), maxX - minX + padding * 2),
    height: Math.min(height - Math.max(0, minY - padding), maxY - minY + padding * 2),
  };

  if (box.width < 60 || box.height < 60) {
    return { score: state.score, box: null };
  }

  return { score: state.score, box };
}

async function initializeCamera() {
  const video = $("#live-video");
  const canvas = $("#vision-canvas");
  const toggle = $("#record-toggle");

  if (!video || !canvas || !toggle) return;

  const cameraState = $("#camera-state");
  const motionIndex = $("#motion-index");
  const faceState = $("#face-state");

  let recorder = null;
  let chunks = [];
  let motionState = {
    background: null,
    frameCount: 0,
    score: 0,
  };
  let stableFaceBox = null;
  let stableMotionBox = null;
  let missedMotionFrames = 0;
  let lastFaceScan = 0;
  let missedFaceFrames = 0;
  let recordingMotionPeak = 0;
  let recordingHadMotion = false;
  let recordingCanvas = null;
  let recordingCtx = null;
  let recordingFrameId = null;
  let activeOverlay = null;

  const faceDetector = "FaceDetector" in window
    ? new FaceDetector({ fastMode: true, maxDetectedFaces: 4 })
    : null;

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    cameraState.textContent = "Unsupported";
    faceState.textContent = "Use Chrome or Edge";
    return;
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: {
        width: { ideal: 1280 },
        height: { ideal: 720 },
        facingMode: "user",
      },
      audio: false,
    });

    video.srcObject = stream;
    await video.play();

    cameraState.textContent = "Live";
    faceState.textContent = faceDetector ? "Face detector ready" : "Motion fallback";

    toggle.addEventListener("click", async () => {
      if (recorder && recorder.state === "recording") {
        recorder.stop();
        toggle.textContent = "Start Recording";
        toggle.classList.add("primary");

        const ctx = canvas.getContext("2d");
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        return;
      }

      chunks = [];
      recordingMotionPeak = 0;
      recordingHadMotion = false;
      activeOverlay = null;

      recordingCanvas = document.createElement("canvas");
      recordingCanvas.width = video.videoWidth || 1280;
      recordingCanvas.height = video.videoHeight || 720;
      recordingCtx = recordingCanvas.getContext("2d");

      const mimeType = MediaRecorder.isTypeSupported("video/webm") ? "video/webm" : "";
      const recordedStream = recordingCanvas.captureStream(30);
      recorder = new MediaRecorder(recordedStream, mimeType ? { mimeType } : undefined);

      recorder.ondataavailable = (event) => {
        if (event.data.size) chunks.push(event.data);
      };

      recorder.onstop = async () => {
        if (recordingFrameId) {
          cancelAnimationFrame(recordingFrameId);
          recordingFrameId = null;
        }

        const blob = new Blob(chunks, { type: "video/webm" });
        const form = new FormData();

        form.append("video", blob, `cctv-${Date.now()}.webm`);
        form.append(
          "source_signature",
          `${$("#camera-signature").textContent} - ${
            recordingHadMotion ? "MOTION DETECTED" : "NO MOTION DETECTED"
          } - peak ${recordingMotionPeak.toFixed(2)}`
        );
        form.append(
          "title",
          `${recordingHadMotion ? "Motion Detected" : "Manual"} Capture ${new Date().toLocaleString()}`
        );

        const csrfToken = document.querySelector('meta[name="csrf-token"]');
        if (csrfToken) {
          form.append("csrf_token", csrfToken.content);
        }

        await fetch("/api/recordings", {
          method: "POST",
          body: form,
        });
      };

      const drawRecordingFrame = () => {
        if (!recorder || recorder.state !== "recording" || !recordingCtx) return;

        drawMirroredVideo(recordingCtx, video, recordingCanvas.width, recordingCanvas.height);

        if (activeOverlay) {
          drawDetectionBox(
            recordingCtx,
            activeOverlay.box,
            activeOverlay.label,
            activeOverlay.color
          );
        }

        recordingFrameId = requestAnimationFrame(drawRecordingFrame);
      };

      await fetch("/api/recording/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source: $("#camera-signature").textContent,
        }),
      });

      recorder.start();
      drawRecordingFrame();
      toggle.textContent = "Stop Recording";
      toggle.classList.remove("primary");
    });

    const scan = async () => {
      if (!video.videoWidth || !video.videoHeight) {
        requestAnimationFrame(scan);
        return;
      }

      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;

      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

      const frame = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const motion = updateMotionState(frame, canvas.width, canvas.height, motionState);

      motionIndex.textContent = motion.score.toFixed(2);

      if (recorder && recorder.state === "recording") {
        recordingMotionPeak = Math.max(recordingMotionPeak, motion.score);
        if (motion.score >= 6 || motion.box) {
          recordingHadMotion = true;
        }
      }

      ctx.clearRect(0, 0, canvas.width, canvas.height);
      activeOverlay = null;

      if (faceDetector && performance.now() - lastFaceScan > 260) {
        lastFaceScan = performance.now();

        try {
          const faces = await faceDetector.detect(video);

          if (faces.length) {
            const largestFace = faces
              .map((face) => face.boundingBox)
              .sort((a, b) => b.width * b.height - a.width * a.height)[0];

            stableFaceBox = smoothBox(stableFaceBox, largestFace);
            missedFaceFrames = 0;
          } else {
            missedFaceFrames += 1;
            if (missedFaceFrames > 6) stableFaceBox = null;
          }
        } catch {
          stableFaceBox = null;
        }
      }

      if (motion.box) {
        missedMotionFrames = 0;

        if (
          stableMotionBox &&
          boxDistance(stableMotionBox, motion.box) > canvas.width * 0.28
        ) {
          stableMotionBox = smoothBox(stableMotionBox, motion.box, 0.92);
        } else {
          stableMotionBox = smoothBox(stableMotionBox, motion.box, 0.86);
        }
      } else {
        missedMotionFrames += 1;
        if (missedMotionFrames > 10) {
          stableMotionBox = null;
        }
      }

      if (stableFaceBox) {
        activeOverlay = {
          box: mirrorBox(stableFaceBox, canvas.width),
          label: "FACE SIGNATURE",
          color: "#00d99a",
        };
        drawDetectionBox(ctx, activeOverlay.box, activeOverlay.label, activeOverlay.color);
        faceState.textContent = "Face detected";
      } else if (stableMotionBox) {
        activeOverlay = {
          box: mirrorBox(stableMotionBox, canvas.width),
          label: "MOTION TARGET",
          color: "#f2a93b",
        };
        drawDetectionBox(ctx, activeOverlay.box, activeOverlay.label, activeOverlay.color);
        faceState.textContent = faceDetector ? "Motion only" : "Motion fallback";
      } else {
        faceState.textContent = faceDetector ? "Scanning" : "Motion fallback";
      }

      requestAnimationFrame(scan);
    };

    scan();
  } catch (error) {
    cameraState.textContent = "Unavailable";

    if (error.name === "NotAllowedError") {
      faceState.textContent = "Camera permission blocked";
    } else if (error.name === "NotFoundError") {
      faceState.textContent = "No camera found";
    } else {
      faceState.textContent = error.name || "Camera error";
    }
  }
}

initializeCamera();
