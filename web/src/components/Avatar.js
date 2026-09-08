import { useState } from 'react';

function Avatar() {
  const [file, setFile] = useState(null);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadSuccess, setUploadSuccess] = useState(false);

  const BASE_URL = import.meta.env.VITE_API_URL || '/api';
  const allowedTypes = {
    'image/png': 'png',
    'image/jpeg': 'jpeg',
    'image/jpg': 'jpg',
  };

  const handleUpload = async () => {
    if (!file) return alert('Please select a file first');

    if (!Object.keys(allowedTypes).includes(file.type)) {
      return alert('Please upload image in PNG or JPEG/JPG format');
    }

    setIsUploading(true);
    setUploadSuccess(false);

    const searchParams = new URLSearchParams(window.location.search);
    const botName = searchParams.get('bot_name');
    const conversationId = searchParams.get('conversation_id');
    const participantId = searchParams.get('participant_id');
    // Log parameters for debugging
    // console.log('🔧 Params:', { botName, conversationId, participantId });

    try {
      // 1. Get presigned URL
      const newFileName = `${participantId}_${conversationId}.${allowedTypes[file.type]}`;

      const res = await fetch(
        `${BASE_URL}/avatar-upload/?filename=${encodeURIComponent(newFileName)}&content_type=${encodeURIComponent(file.type)}`
      );
      const { s3_url } = await res.json();

      // 2. Upload to S3
      const upload = await fetch(s3_url, {
        method: 'PUT',
        headers: {
          'Content-Type': file.type,
        },
        body: file,
      });

      if (!upload.ok) throw new Error('Upload failed.');
      // console.log('Upload successful!');

      // 3. Register avatar with backend
      const imageUpload = await fetch(`${BASE_URL}/avatar/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          bot_name: botName,
          participant_id: participantId,
          conversation_id: conversationId,
          image_path: newFileName,
        }),
      });
      // console.log(imageUpload);

      if (!imageUpload.ok) {
        throw new Error(`Failed to create avatar for bot ${botName}`);
      }

      setUploadSuccess(true);
    } catch (err) {
      // console.error('Error during upload:', err);
    } finally {
      setIsUploading(false);
    }
  };

  return (
    <div>
      {!isUploading && !uploadSuccess && (
        <>
          <input
            type="file"
            accept="image/*"
            onChange={e => setFile(e.target.files[0])}
          />
          <button onClick={handleUpload} disabled={isUploading}>
            Upload
          </button>
        </>
      )}

      {isUploading && <div>Uploading, Please wait a few seconds.</div>}

      {uploadSuccess && (
        <div style={{ marginTop: '1rem', color: 'green' }}>
          ✅ Upload successful! You can now click **Next**.
        </div>
      )}
    </div>
  );
}

export default Avatar;
