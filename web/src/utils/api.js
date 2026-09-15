import axios from 'axios';

// CloudFront serves the app and proxies /api/* to the backend on the same
// host, and the dev server proxies /api the same way, so a relative base
// works in both. VITE_API_URL stays supported as an override for setups that
// serve the API from somewhere else.
const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '/api',
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
});

api.interceptors.request.use(
  config => {
    const token = localStorage.getItem('token');
    if (token) {
      config.headers['Authorization'] = `Token ${token}`;
    }
    return config;
  },
  error => {
    return Promise.reject(error);
  }
);

export async function fetchData(url = '') {
  const response = await api.get(url);
  return response.data;
}

export async function postData(url = '', body = {}) {
  const response = await api.post(url, body);
  return response.data;
}

export async function putData(url = '', body = {}) {
  const response = await api.put(url, body);
  return response.data;
}

export async function deleteData(url = '') {
  const response = await api.delete(url);
  return response.data;
}

export async function createChatSession(moduleId, taskId) {
  const response = await postData('/chat_sessions/', {
    module: moduleId,
    task: taskId,
  });
  return response;
}

export async function sendMessage(sessionId, message, sender) {
  const response = await postData('/chat_messages/', {
    session: sessionId,
    message,
    sender,
  });
  return response;
}

export const createWebSocket = (sessionId, isAudioMode) => {
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
  // CloudFront routes /ws/* to the backend on the same host that serves the
  // app, so derive the socket origin from the page. Deriving it from the API
  // base URL instead breaks as soon as that base is relative, and produced
  // /api/ws/... — a path CloudFront does not route — when it was absolute.
  const wsUrl = `${protocol}://${window.location.host}`;
  const endpoint = isAudioMode
    ? `/ws/audio/${sessionId}/`
    : `/ws/chat/${sessionId}/`;
  return new WebSocket(`${wsUrl}${endpoint}`);
};

export const postFile = async (filePath, formData) => {
  const response = await api.post(filePath, formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  });
  return response.data;
};

export async function fetchFile(url = '') {
  const response = await api.get(url, { responseType: 'blob' });
  return response.data;
}

export async function getPresignedUrl(fileName, fileType, isAvatar = false) {
  const urlPath = isAvatar ? '/get-avatar-url/' : '/generate_presigned_url/';
  const response = await api.post(urlPath, {
    file_name: fileName,
    file_type: fileType,
    is_avatar: isAvatar,
  });
  return response.data;
}

export async function getPresignedUrlForDisplay(fileName) {
  const response = await api.get(
    `/generate_presigned_url/?file_name=${encodeURIComponent(fileName)}`
  );
  // console.log('Response:', response.data);
  return response.data;
}

export async function getLocalFile(fileName) {
  // console.log('Getting local file:', fileName);
  const response = await api.get(`/local_upload/?file_name=${fileName}`, {
    responseType: 'blob', // Treat the response as a Blob
  });
  return response.data;
}

export async function uploadToS3(url, fields, file) {
  const formData = new FormData();
  Object.entries({ ...fields, file }).forEach(([key, value]) => {
    formData.append(key, value);
  });

  await axios.post(url, formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  });
}

export default api;
