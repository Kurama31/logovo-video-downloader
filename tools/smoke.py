"""End-to-end check against a running local Compose stack (small public sample)."""
import time

import httpx


def main():
    origin = 'http://localhost:8080'
    headers = {'Origin': origin, 'X-Requested-With': 'DAV'}
    with httpx.Client(base_url=origin, timeout=60, trust_env=False) as client:
        assert client.get('/health').status_code == 200
        response = client.post('/api/jobs', headers=headers,
                               json={'url': 'https://www.w3schools.com/html/mov_bbb.mp4', 'quality': 1080})
        response.raise_for_status()
        job_id = response.json()['id']
        try:
            for _ in range(60):
                jobs = client.get('/api/jobs').json()
                job = next(j for j in jobs if j['id'] == job_id)
                if job['status'] in ('awaiting', 'error'):
                    break
                time.sleep(1)
            assert job['status'] == 'awaiting', job
            result = client.post(f'/api/jobs/{job_id}/start', headers=headers)
            result.raise_for_status()
            assert result.json()['status'] == 'client_downloading'
            plan=client.get(f'/api/jobs/{job_id}/client/plan')
            plan.raise_for_status()
            assert plan.json()['remux'] is False
            endpoint=f'/api/jobs/{job_id}/client/tracks/0/parts/0'
            video = client.get(endpoint)
            video.raise_for_status()
            assert b'ftyp' in video.content[:64] and len(video.content) > 10000
            etag=video.headers.get('etag','')
            validator=etag if etag and not etag.startswith('W/') else video.headers.get('last-modified')
            partial = client.get(endpoint, headers={'Range': 'bytes=1000-','If-Range':validator})
            partial.raise_for_status()
            assert partial.status_code == 206 and partial.content == video.content[1000:]
            client.post(f'/api/jobs/{job_id}/pause', headers=headers).raise_for_status()
            assert client.get(endpoint).status_code == 409
            client.post(f'/api/jobs/{job_id}/resume', headers=headers).raise_for_status()
            client.post(f'/api/jobs/{job_id}/client/progress',headers=headers,
                        json={'downloaded':len(video.content),'total':len(video.content),'phase':'complete'}).raise_for_status()
            print(f'Compose V2 E2E OK: {len(video.content)} bytes; exact Range; pause/resume; browser completion.')
        finally:
            client.post(f'/api/jobs/{job_id}/cancel', headers=headers)


if __name__ == '__main__':
    main()
