FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*


WORKDIR /app

# Face detection (YuNet) and embedding (SFace) models, Apache-2.0, pinned.
ARG OPENCV_ZOO=https://github.com/opencv/opencv_zoo/raw/47534e27c9851bb1128ccc0102f1145e27f23f98/models
RUN mkdir -p /models \
 && curl -fsSL -o /models/face_detection_yunet_2023mar.onnx \
      $OPENCV_ZOO/face_detection_yunet/face_detection_yunet_2023mar.onnx \
 && curl -fsSL -o /models/face_recognition_sface_2021dec.onnx \
      $OPENCV_ZOO/face_recognition_sface/face_recognition_sface_2021dec.onnx \
 && echo "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4  /models/face_detection_yunet_2023mar.onnx" | sha256sum -c - \
 && echo "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79  /models/face_recognition_sface_2021dec.onnx" | sha256sum -c -

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/



CMD ["gunicorn", "-w", "2", "-k", "gthread", "--threads", "4", "-b", "0.0.0.0:8383", "--timeout", "300", "app:create_app()"]
